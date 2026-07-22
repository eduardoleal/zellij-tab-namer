use std::collections::{hash_map::DefaultHasher, BTreeMap, HashMap};
use std::hash::{Hash, Hasher};

use crate::llm::{normalize_source, validate_chat_response, ResponseError, MAX_SOURCE_BYTES};

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

const MAX_GLOBAL_REQUESTS: usize = 2;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScheduledRefinement {
    pub request_id: u64,
    pub source: String,
    pub max_chars: usize,
}

#[derive(Debug, PartialEq, Eq)]
pub struct RefinementDecision {
    pub generation: u64,
    pub rename: RenameDecision,
    pub requests: Vec<ScheduledRefinement>,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct RefinementCompletion {
    pub recognized: bool,
    pub response_error: Option<ResponseError>,
    pub rename: Option<(usize, String)>,
    pub requests: Vec<ScheduledRefinement>,
}

#[derive(Clone, Debug)]
struct QueuedRefinement {
    generation: u64,
    source: String,
    max_chars: usize,
    sequence: u64,
}

#[derive(Clone, Debug)]
struct RefinementTab {
    generation: u64,
    source: String,
    source_is_eligible: bool,
    generated: String,
    observed_name: String,
    max_chars: usize,
    enabled: bool,
    manual: bool,
    terminal: bool,
    in_flight: Option<u64>,
    queued: Option<QueuedRefinement>,
}

#[derive(Clone, Debug)]
struct RequestRecord {
    tab_id: usize,
    generation: u64,
    source: String,
    max_chars: usize,
}

#[derive(Default)]
pub struct RefinementCoordinator {
    tabs: BTreeMap<usize, RefinementTab>,
    requests: HashMap<u64, RequestRecord>,
    next_request_id: u64,
    next_generation: u64,
    next_queue_sequence: u64,
}

impl RefinementCoordinator {
    pub fn reconcile(
        &mut self,
        tab_id: usize,
        current_name: &str,
        source: &str,
        fallback: &str,
        max_chars: usize,
        enabled: bool,
    ) -> RefinementDecision {
        let (source, source_is_eligible) = retained_source_identity(source);
        let source_changed = self
            .tabs
            .get(&tab_id)
            .is_none_or(|tab| tab.source != source);

        if source_changed {
            self.next_generation = self.next_generation.wrapping_add(1).max(1);
            let generation = self.next_generation;
            let old = self.tabs.remove(&tab_id);
            let manual = old.as_ref().is_some_and(|tab| tab.manual)
                || old.as_ref().is_some_and(|tab| {
                    current_name != tab.observed_name
                        && is_manual_name(current_name, Some(&tab.generated), fallback)
                })
                || (old.is_none() && is_manual_name(current_name, None, fallback));
            let in_flight = old.and_then(|tab| tab.in_flight);
            self.tabs.insert(
                tab_id,
                RefinementTab {
                    generation,
                    source: source.clone(),
                    source_is_eligible,
                    generated: fallback.to_owned(),
                    observed_name: current_name.to_owned(),
                    max_chars,
                    enabled,
                    manual,
                    terminal: false,
                    in_flight,
                    queued: None,
                },
            );
        } else if let Some(tab) = self.tabs.get_mut(&tab_id) {
            tab.observed_name = current_name.to_owned();
            tab.enabled = enabled;
            tab.max_chars = max_chars;
            if is_manual_name(current_name, Some(&tab.generated), fallback) {
                tab.manual = true;
                tab.queued = None;
            }
        }

        let rename = {
            let tab = self.tabs.get(&tab_id).expect("tab was just inserted");
            if tab.manual {
                RenameDecision::SkipManual
            } else if tab.generated.is_empty() {
                RenameDecision::NoCandidate
            } else if current_name == tab.generated {
                RenameDecision::AlreadyNamed
            } else {
                RenameDecision::Rename(tab.generated.clone())
            }
        };

        self.queue_if_eligible(tab_id);
        RefinementDecision {
            generation: self.tabs[&tab_id].generation,
            rename,
            requests: self.dispatch_queued(),
        }
    }

    pub fn finish(
        &mut self,
        request_id: u64,
        status: u16,
        body: &[u8],
        live_tab_names: &BTreeMap<usize, String>,
    ) -> RefinementCompletion {
        let Some(request) = self.requests.remove(&request_id) else {
            return RefinementCompletion::default();
        };

        let mut response_error = None;
        let mut rename = None;
        if let Some(tab) = self.tabs.get_mut(&request.tab_id) {
            if tab.in_flight == Some(request_id) {
                tab.in_flight = None;
            }
            if tab.generation == request.generation && tab.source == request.source {
                tab.terminal = true;
                tab.queued = None;
                if let Some(live_name) = live_tab_names.get(&request.tab_id) {
                    tab.observed_name.clone_from(live_name);
                    if is_manual_name(live_name, Some(&tab.generated), &tab.generated) {
                        tab.manual = true;
                    } else if tab.enabled && !tab.manual && live_name == &tab.generated {
                        match validate_chat_response(status, body, request.max_chars) {
                            Ok(label) if label != tab.generated => {
                                tab.generated.clone_from(&label);
                                rename = Some((request.tab_id, label));
                            }
                            Ok(_) => {}
                            Err(error) => response_error = Some(error),
                        }
                    }
                }
            }
        }

        RefinementCompletion {
            recognized: true,
            response_error,
            rename,
            requests: self.dispatch_queued(),
        }
    }

    pub fn retain_tabs(&mut self, active_tab_ids: &[usize]) -> Vec<ScheduledRefinement> {
        self.tabs
            .retain(|tab_id, _| active_tab_ids.contains(tab_id));
        self.dispatch_queued()
    }

    pub fn in_flight_count(&self) -> usize {
        self.requests.len()
    }

    pub fn queued_count(&self) -> usize {
        self.tabs
            .values()
            .filter(|tab| tab.queued.is_some())
            .count()
    }

    pub fn tab_count(&self) -> usize {
        self.tabs.len()
    }

    fn queue_if_eligible(&mut self, tab_id: usize) {
        let in_flight_is_current = self.tabs.get(&tab_id).is_some_and(|tab| {
            tab.in_flight
                .and_then(|request_id| self.requests.get(&request_id))
                .is_some_and(|request| {
                    request.generation == tab.generation && request.source == tab.source
                })
        });
        let Some(tab) = self.tabs.get_mut(&tab_id) else {
            return;
        };
        let eligible = tab.enabled
            && !tab.manual
            && !tab.terminal
            && tab.observed_name == tab.generated
            && tab.source_is_eligible
            && !tab.source.is_empty()
            && tab.source.chars().count() > tab.max_chars
            && tab.max_chars > 0;
        if !eligible {
            tab.queued = None;
            return;
        }
        if in_flight_is_current {
            tab.queued = None;
            return;
        }
        if let Some(queued) = &mut tab.queued {
            queued.generation = tab.generation;
            queued.source.clone_from(&tab.source);
            queued.max_chars = tab.max_chars;
        } else {
            self.next_queue_sequence = self.next_queue_sequence.wrapping_add(1).max(1);
            tab.queued = Some(QueuedRefinement {
                generation: tab.generation,
                source: tab.source.clone(),
                max_chars: tab.max_chars,
                sequence: self.next_queue_sequence,
            });
        }
    }

    fn dispatch_queued(&mut self) -> Vec<ScheduledRefinement> {
        let mut dispatched = Vec::new();
        while self.requests.len() < MAX_GLOBAL_REQUESTS {
            let next_tab = self
                .tabs
                .iter()
                .filter(|(_, tab)| !tab.manual && !tab.terminal && tab.in_flight.is_none())
                .filter_map(|(tab_id, tab)| {
                    tab.queued.as_ref().map(|queued| (*tab_id, queued.sequence))
                })
                .min_by_key(|(tab_id, sequence)| (*sequence, *tab_id))
                .map(|(tab_id, _)| tab_id);
            let Some(tab_id) = next_tab else {
                break;
            };
            let tab = self.tabs.get_mut(&tab_id).expect("queued tab exists");
            let queued = tab.queued.take().expect("queued work exists");
            if queued.generation != tab.generation || queued.source != tab.source {
                continue;
            }
            self.next_request_id = self.next_request_id.wrapping_add(1).max(1);
            let request_id = self.next_request_id;
            tab.in_flight = Some(request_id);
            self.requests.insert(
                request_id,
                RequestRecord {
                    tab_id,
                    generation: queued.generation,
                    source: queued.source.clone(),
                    max_chars: queued.max_chars,
                },
            );
            dispatched.push(ScheduledRefinement {
                request_id,
                source: queued.source,
                max_chars: queued.max_chars,
            });
        }
        dispatched
    }
}

fn retained_source_identity(source: &str) -> (String, bool) {
    let normalized = normalize_source(source);
    if normalized.len() <= MAX_SOURCE_BYTES {
        return (normalized, true);
    }

    let mut hasher = DefaultHasher::new();
    normalized.hash(&mut hasher);
    (
        format!("oversized:{}:{:016x}", normalized.len(), hasher.finish()),
        false,
    )
}

fn is_manual_name(current_name: &str, generated: Option<&str>, fallback: &str) -> bool {
    !is_default_tab_name(current_name)
        && generated != Some(current_name)
        && current_name != fallback
}

pub fn choose_pane(panes: &[PaneSnapshot]) -> Option<&PaneSnapshot> {
    panes.iter().max_by_key(|pane| pane_score(pane))
}

pub fn label_for_pane(pane: &PaneSnapshot, max_chars: usize) -> String {
    shorten_label(&canonical_source_for_pane(pane), max_chars)
}

pub fn canonical_source_for_pane(pane: &PaneSnapshot) -> String {
    let title = clean_title(&pane.title);
    let command = command_name(&pane.command);
    if is_useful_title(&title, &command) {
        return title;
    }

    let project = project_name(&pane.cwd).or_else(|| project_name(&title));
    match (project, command.as_str()) {
        (Some(project), "") => project.to_owned(),
        (Some(project), command) => format!("{project} - {command}"),
        (None, command) => command.to_owned(),
    }
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
    let normalized = normalize_source(label);
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
        assert_eq!(
            canonical_source_for_pane(&input),
            "Implement native WASM tab naming"
        );
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
    fn refinement_is_fallback_first_and_globally_bounded() {
        let mut coordinator = RefinementCoordinator::default();
        let first = coordinator.reconcile(
            1,
            "Tab #2",
            "implement native ollama compression safely",
            "implement native ollama...",
            24,
            true,
        );
        let second = coordinator.reconcile(
            2,
            "Tab #3",
            "review installer permission migration carefully",
            "review installer...",
            24,
            true,
        );
        let third = coordinator.reconcile(
            3,
            "Tab #4",
            "document native and watcher configuration separately",
            "document native and...",
            24,
            true,
        );

        assert_eq!(
            first.rename,
            RenameDecision::Rename("implement native ollama...".into())
        );
        assert!(first.requests.is_empty());
        assert!(second.requests.is_empty());
        assert!(third.requests.is_empty());

        let first = coordinator.reconcile(
            1,
            "implement native ollama...",
            "implement native ollama compression safely",
            "implement native ollama...",
            24,
            true,
        );
        let second = coordinator.reconcile(
            2,
            "review installer...",
            "review installer permission migration carefully",
            "review installer...",
            24,
            true,
        );
        coordinator.reconcile(
            3,
            "document native and...",
            "document native and watcher configuration separately",
            "document native and...",
            24,
            true,
        );
        assert_eq!(first.requests.len(), 1);
        assert_eq!(second.requests.len(), 1);
        assert_eq!(coordinator.in_flight_count(), 2);
        assert_eq!(coordinator.queued_count(), 1);
    }

    fn response(label: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "choices": [{"message": {"content": label}}]
        }))
        .unwrap()
    }

    fn live_name(tab_id: usize, name: &str) -> BTreeMap<usize, String> {
        BTreeMap::from([(tab_id, name.to_owned())])
    }

    #[test]
    fn valid_response_requires_observed_fallback_and_never_churns() {
        let mut coordinator = RefinementCoordinator::default();
        let first = coordinator.reconcile(
            7,
            "Tab #8",
            "implement native ollama compression safely",
            "implement native...",
            20,
            true,
        );
        assert!(first.requests.is_empty());
        let observed = coordinator.reconcile(
            7,
            "implement native...",
            "implement native ollama compression safely",
            "implement native...",
            20,
            true,
        );
        let request_id = observed.requests[0].request_id;
        let completion = coordinator.finish(
            request_id,
            200,
            &response("native Ollama"),
            &live_name(7, "implement native..."),
        );
        assert_eq!(completion.rename, Some((7, "native Ollama".into())));

        let stable = coordinator.reconcile(
            7,
            "native Ollama",
            "implement native ollama compression safely",
            "implement native...",
            20,
            true,
        );
        assert_eq!(stable.rename, RenameDecision::AlreadyNamed);
        assert!(stable.requests.is_empty());
    }

    #[test]
    fn request_waits_for_fallback_observation() {
        let mut coordinator = RefinementCoordinator::default();
        let decision = coordinator.reconcile(
            8,
            "Tab #9",
            "review delayed result ordering carefully",
            "review delayed...",
            18,
            true,
        );
        assert!(decision.requests.is_empty());
        let observed = coordinator.reconcile(
            8,
            "review delayed...",
            "review delayed result ordering carefully",
            "review delayed...",
            18,
            true,
        );
        assert_eq!(observed.requests.len(), 1);
    }

    #[test]
    fn manual_rename_during_flight_wins_for_tab_lifetime() {
        let mut coordinator = RefinementCoordinator::default();
        let initial = coordinator.reconcile(
            9,
            "Tab #10",
            "protect manual production deployment label",
            "protect manual...",
            18,
            true,
        );
        assert!(initial.requests.is_empty());
        let decision = coordinator.reconcile(
            9,
            "protect manual...",
            "protect manual production deployment label",
            "protect manual...",
            18,
            true,
        );
        assert_eq!(
            coordinator
                .finish(
                    decision.requests[0].request_id,
                    200,
                    &response("manual safety"),
                    &live_name(9, "prod deploy"),
                )
                .rename,
            None
        );
        let changed = coordinator.reconcile(
            9,
            "prod deploy",
            "a completely different long source title",
            "different source...",
            18,
            true,
        );
        assert_eq!(changed.rename, RenameDecision::SkipManual);
        assert!(changed.requests.is_empty());
    }

    #[test]
    fn stale_closed_unknown_and_cross_tab_results_are_ignored() {
        let mut coordinator = RefinementCoordinator::default();
        coordinator.reconcile(
            11,
            "Tab #12",
            "first source requiring model compression",
            "first source...",
            16,
            true,
        );
        let first = coordinator.reconcile(
            11,
            "first source...",
            "first source requiring model compression",
            "first source...",
            16,
            true,
        );
        coordinator.reconcile(
            11,
            "first source...",
            "second source replacing queued generation",
            "second source...",
            16,
            true,
        );
        let stale = coordinator.finish(
            first.requests[0].request_id,
            200,
            &response("wrong tab"),
            &live_name(11, "second source..."),
        );
        assert_eq!(stale.rename, None);
        assert!(stale.requests.is_empty());
        let current = coordinator.reconcile(
            11,
            "second source...",
            "second source replacing queued generation",
            "second source...",
            16,
            true,
        );
        let current_request = current.requests[0].request_id;
        coordinator.retain_tabs(&[]);
        assert_eq!(
            coordinator
                .finish(
                    current_request,
                    200,
                    &response("closed tab"),
                    &BTreeMap::new()
                )
                .rename,
            None
        );
        assert_eq!(
            coordinator
                .finish(999_999, 200, &response("unknown"), &BTreeMap::new())
                .rename,
            None
        );
        assert_eq!(coordinator.tab_count(), 0);
    }

    #[test]
    fn source_churn_coalesces_latest_generation_and_state_stays_bounded() {
        let mut coordinator = RefinementCoordinator::default();
        coordinator.reconcile(
            21,
            "Tab #22",
            "initial title long enough to request inference",
            "initial title...",
            16,
            true,
        );
        let first = coordinator.reconcile(
            21,
            "initial title...",
            "initial title long enough to request inference",
            "initial title...",
            16,
            true,
        );
        let request_id = first.requests[0].request_id;
        let mut current_name = "initial title...";
        for index in 0..2_000 {
            let source = format!("latest changing source generation number {index}");
            coordinator.reconcile(21, current_name, &source, "latest changing...", 18, true);
            current_name = "latest changing...";
            coordinator.reconcile(
                21,
                "latest changing...",
                &source,
                "latest changing...",
                18,
                true,
            );
            assert!(coordinator.in_flight_count() <= 2);
            assert!(coordinator.queued_count() <= coordinator.tab_count());
        }
        assert_eq!(coordinator.in_flight_count(), 1);
        assert_eq!(coordinator.queued_count(), 1);
        let completion =
            coordinator.finish(request_id, 503, b"", &live_name(21, "latest changing..."));
        assert_eq!(completion.requests.len(), 1);
        assert!(completion.requests[0].source.ends_with("1999"));
    }

    #[test]
    fn short_disabled_and_oversized_sources_never_schedule() {
        let mut coordinator = RefinementCoordinator::default();
        for (tab_id, source, enabled) in [
            (30, "short", true),
            (31, "long but explicitly disabled source", false),
        ] {
            assert!(coordinator
                .reconcile(tab_id, "Tab #1", source, source, 10, enabled)
                .requests
                .is_empty());
        }
        let oversized = "x".repeat(crate::llm::MAX_SOURCE_BYTES + 1);
        assert!(coordinator
            .reconcile(32, "Tab #1", &oversized, "oversized...", 10, true)
            .requests
            .is_empty());
        let retained = &coordinator.tabs[&32].source;
        assert!(retained.len() < 64);
        assert!(retained.starts_with("oversized:"));
    }
}
