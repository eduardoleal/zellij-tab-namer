use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaneSnapshot {
    pub title: String,
    pub command: String,
    pub cwd: String,
    pub focused: bool,
    pub floating: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RenameDecision {
    Rename(String),
    AlreadyNamed,
    SkipManual,
    NoCandidate,
}

#[derive(Default)]
pub struct NamingState {
    generated: HashMap<usize, String>,
}

impl NamingState {
    pub fn decision(
        &mut self,
        tab_id: usize,
        current_name: &str,
        pane: &PaneSnapshot,
        max_chars: usize,
    ) -> RenameDecision {
        let candidate = label_for_pane(pane, max_chars);
        if candidate.is_empty() {
            return RenameDecision::NoCandidate;
        }

        let last_generated = self.generated.get(&tab_id);
        let is_manual = !is_default_tab_name(current_name)
            && last_generated.map(String::as_str) != Some(current_name)
            && current_name != candidate;
        if is_manual {
            return RenameDecision::SkipManual;
        }

        self.generated.insert(tab_id, candidate.clone());
        if current_name == candidate {
            RenameDecision::AlreadyNamed
        } else {
            RenameDecision::Rename(candidate)
        }
    }

    pub fn retain_tabs(&mut self, active_tab_ids: &[usize]) {
        self.generated
            .retain(|tab_id, _| active_tab_ids.contains(tab_id));
    }
}

pub fn choose_pane(panes: &[PaneSnapshot]) -> Option<&PaneSnapshot> {
    panes.iter().max_by_key(|pane| pane_score(pane))
}

pub fn label_for_pane(pane: &PaneSnapshot, max_chars: usize) -> String {
    let title = clean_title(&pane.title);
    let command = command_name(&pane.command);
    if is_useful_title(&title, &command) {
        return shorten_label(&title, max_chars);
    }

    let project = project_name(&pane.cwd).or_else(|| project_name(&title));
    let fallback = match (project, command.as_str()) {
        (Some(project), "") => project.to_owned(),
        (Some(project), command) => format!("{project} - {command}"),
        (None, command) => command.to_owned(),
    };
    shorten_label(&fallback, max_chars)
}

pub fn is_default_tab_name(name: &str) -> bool {
    let name = name.trim();
    if name.is_empty() || name.chars().all(|character| character.is_ascii_digit()) {
        return true;
    }
    name.strip_prefix("Tab #")
        .is_some_and(|suffix| !suffix.is_empty() && suffix.chars().all(|c| c.is_ascii_digit()))
}

fn clean_title(title: &str) -> String {
    let collapsed = title.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed
        .trim_start_matches(|character: char| {
            !character.is_alphanumeric() && !matches!(character, '~' | '/' | '.')
        })
        .trim()
        .to_owned()
}

fn is_useful_title(title: &str, command: &str) -> bool {
    if title.is_empty() || looks_like_path(title) {
        return false;
    }
    let lower = title.to_ascii_lowercase();
    let is_default_pane_name = lower
        .strip_prefix("pane #")
        .is_some_and(|suffix| !suffix.is_empty() && suffix.chars().all(|c| c.is_ascii_digit()));
    !matches!(
        lower.as_str(),
        "bash" | "zsh" | "fish" | "sh" | "vim" | "nvim" | "hx" | "helix" | "claude code" | "codex"
    ) && !is_default_pane_name
        && lower != command.to_ascii_lowercase()
}

fn pane_score(pane: &PaneSnapshot) -> usize {
    let title = clean_title(&pane.title);
    let command = command_name(&pane.command);
    let mut score = if is_useful_title(&title, &command) {
        100
    } else if looks_like_path(&title) {
        20
    } else {
        10
    };
    if pane.focused {
        score += 3;
    }
    if !pane.floating {
        score += 2;
    }
    score
}

fn command_name(command: &str) -> String {
    let executable = command.split_whitespace().next().unwrap_or_default();
    executable
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or_default()
        .to_owned()
}

fn project_name(path: &str) -> Option<&str> {
    if !looks_like_path(path) {
        return None;
    }
    path.trim_end_matches('/')
        .rsplit('/')
        .next()
        .filter(|name| !name.is_empty())
}

fn looks_like_path(value: &str) -> bool {
    value.starts_with("~/")
        || value.starts_with('/')
        || value.contains("/.")
        || (value.contains('/') && !value.contains(' '))
}

fn shorten_label(label: &str, max_chars: usize) -> String {
    let normalized = label.split_whitespace().collect::<Vec<_>>().join(" ");
    let length = normalized.chars().count();
    if max_chars == 0 {
        return String::new();
    }
    if length <= max_chars {
        return normalized;
    }
    if max_chars <= 3 {
        return normalized.chars().take(max_chars).collect();
    }

    let limit = max_chars - 3;
    let prefix: String = normalized.chars().take(limit).collect();
    let word_cut = prefix.rfind(' ');
    let prefix = match word_cut {
        Some(index) if index >= 8.max(limit / 2) => prefix[..index].trim_end(),
        _ => prefix.trim_end(),
    };
    format!("{prefix}...")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pane(title: &str, command: &str, cwd: &str) -> PaneSnapshot {
        PaneSnapshot {
            title: title.into(),
            command: command.into(),
            cwd: cwd.into(),
            focused: true,
            floating: false,
        }
    }

    #[test]
    fn useful_title_wins_over_shell_fallback() {
        let input = pane(
            "Implement native WASM tab naming",
            "/opt/homebrew/bin/fish",
            "/Users/eduardoleal/eduardoleal/zellij-tab-namer",
        );

        assert_eq!(label_for_pane(&input, 28), "Implement native WASM...");
    }

    #[test]
    fn generic_title_falls_back_to_project_and_activity() {
        let input = pane(
            "fish",
            "/opt/homebrew/bin/fish",
            "/Users/eduardoleal/.config/zellij",
        );

        assert_eq!(label_for_pane(&input, 32), "zellij - fish");
    }

    #[test]
    fn default_pane_title_falls_back_to_project_and_activity() {
        let input = pane(
            "Pane #1",
            "/opt/homebrew/bin/fish",
            "/Users/eduardoleal/.config/zellij",
        );

        assert_eq!(label_for_pane(&input, 32), "zellij - fish");
    }

    #[test]
    fn useful_non_floating_pane_beats_focused_generic_floating_pane() {
        let mut floating = pane("fish", "fish", "/tmp");
        floating.floating = true;
        let mut task = pane("Fix permission cache", "codex", "/tmp");
        task.focused = false;

        assert_eq!(
            choose_pane(&[floating, task]).unwrap().title,
            "Fix permission cache"
        );
    }

    #[test]
    fn manual_name_is_preserved_until_cleared() {
        let mut state = NamingState::default();
        let input = pane("New task title", "codex", "/tmp/project");

        assert_eq!(
            state.decision(4, "prod deploy", &input, 32),
            RenameDecision::SkipManual
        );
        assert_eq!(
            state.decision(4, "Tab #5", &input, 32),
            RenameDecision::Rename("New task title".into())
        );
    }

    #[test]
    fn generated_names_update_without_churn() {
        let mut state = NamingState::default();
        let first = pane("First task", "codex", "/tmp/project");
        let second = pane("Second task", "codex", "/tmp/project");

        assert_eq!(
            state.decision(5, "Tab #6", &first, 32),
            RenameDecision::Rename("First task".into())
        );
        assert_eq!(
            state.decision(5, "First task", &first, 32),
            RenameDecision::AlreadyNamed
        );
        assert_eq!(
            state.decision(5, "First task", &second, 32),
            RenameDecision::Rename("Second task".into())
        );
    }
}
