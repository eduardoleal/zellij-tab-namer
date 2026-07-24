use std::collections::BTreeMap;

use zellij_tab_namer_plugin::adapter::{Action, Adapter};
use zellij_tab_namer_plugin::naming::{
    canonical_source_for_pane, choose_pane, label_for_pane, PaneSnapshot,
};
use zellij_tile::prelude::*;

const DEBOUNCE_SECONDS: f64 = 0.35;

#[derive(Default)]
struct TabNamer {
    adapter: Option<Adapter>,
    tabs: Vec<TabInfo>,
    panes: PaneManifest,
    update_pending: bool,
    session_role: bool,
    session_name: Option<String>,
    session_input: String,
    session_locked: Option<bool>,
    session_command_generation: u64,
    session_status: String,
    pending_session_name: Option<String>,
}

register_plugin!(TabNamer);

impl ZellijPlugin for TabNamer {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        self.session_role = configuration
            .get("role")
            .is_some_and(|role| role == "session-lock");
        let adapter = Adapter::new(configuration);
        self.execute(adapter.load_actions());
        self.adapter = Some(adapter);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::TabUpdate(tabs) => {
                if self.session_role {
                    return false;
                }
                self.tabs = tabs;
                self.schedule_update();
            }
            Event::PaneUpdate(panes) => {
                if self.session_role {
                    return false;
                }
                self.panes = panes;
                self.schedule_update();
            }
            Event::CwdChanged(..) | Event::CommandChanged(..) if !self.session_role => {
                self.schedule_update()
            }
            Event::Timer(_) => {
                if self.session_role {
                    return false;
                }
                self.update_pending = false;
                self.reconcile_tabs();
            }
            Event::PermissionRequestResult(status) => {
                if let Some(adapter) = &mut self.adapter {
                    adapter.permission_result(status == PermissionStatus::Granted);
                }
                self.schedule_update();
            }
            Event::ModeUpdate(mode) => {
                if let Some(name) = mode.session_name {
                    if self.pending_session_name.as_deref() == Some(name.as_str()) {
                        self.pending_session_name = None;
                        self.session_name = Some(name.clone());
                        self.session_command("mark", &name);
                        return self.session_role;
                    }
                    if self.session_name.as_deref() == Some(name.as_str()) {
                        return self.session_role;
                    }
                    self.session_name = Some(name);
                    self.query_session_lock();
                }
            }
            Event::RunCommandResult(code, stdout, stderr, context) => {
                self.handle_session_command(code, stdout, stderr, context);
                return self.session_role;
            }
            Event::Key(key) if self.session_role => return self.handle_session_key(key),
            Event::PastedText(text) if self.session_role => {
                self.session_input.push_str(&text);
                return true;
            }
            Event::WebRequestResult(status, _, body, context) => {
                let live_tab_names = self
                    .tabs
                    .iter()
                    .map(|tab| (tab.tab_id, tab.name.clone()))
                    .collect();
                let actions = self
                    .adapter
                    .as_mut()
                    .map(|adapter| adapter.web_result(status, &body, &context, &live_tab_names))
                    .unwrap_or_default();
                self.execute(actions);
            }
            _ => {}
        }
        self.session_role
    }

    fn render(&mut self, _rows: usize, _cols: usize) {
        if !self.session_role {
            return;
        }
        match self.session_locked {
            Some(true) => println!(
                "Unlock session: {}\n\nEnter to unlock · Esc to cancel\n{}",
                self.session_name.as_deref().unwrap_or("current"),
                self.session_status
            ),
            Some(false) => println!(
                "Lock session\n\nName: {}\n\nEnter to lock · Esc to cancel\n{}",
                self.session_input, self.session_status
            ),
            None => println!("Checking session lock state…\n{}", self.session_status),
        }
    }
}

impl TabNamer {
    fn schedule_update(&mut self) {
        if !self.update_pending {
            self.update_pending = true;
            set_timeout(DEBOUNCE_SECONDS);
        }
    }

    fn session_command(&mut self, operation: &str, name: &str) {
        let Some(adapter) = &self.adapter else {
            return;
        };
        let command = adapter.session_lock_command();
        let state_file = adapter.session_lock_state_file();
        if state_file.trim().is_empty() {
            return;
        }
        self.session_command_generation += 1;
        let context = BTreeMap::from([
            ("session_lock_operation".to_owned(), operation.to_owned()),
            ("session_lock_name".to_owned(), name.to_owned()),
            (
                "session_lock_generation".to_owned(),
                self.session_command_generation.to_string(),
            ),
        ]);
        run_command(
            &[
                &command,
                "session-lock",
                operation,
                "--state-file",
                &state_file,
                "--",
                name,
            ],
            context,
        );
    }

    fn query_session_lock(&mut self) {
        if let Some(name) = self.session_name.clone() {
            self.session_command("query", &name);
        }
    }

    fn handle_session_command(
        &mut self,
        code: Option<i32>,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
        context: BTreeMap<String, String>,
    ) {
        let Some(operation) = context.get("session_lock_operation") else {
            return;
        };
        let Some(name) = context.get("session_lock_name") else {
            return;
        };
        let Some(generation) = context
            .get("session_lock_generation")
            .and_then(|value| value.parse::<u64>().ok())
        else {
            return;
        };
        if generation != self.session_command_generation {
            return;
        }
        if self.session_name.as_deref() != Some(name.as_str()) {
            return;
        }
        if code != Some(0) {
            let output = if stderr.is_empty() { &stdout } else { &stderr };
            self.session_status = format!(
                "Session lock command failed: {}",
                String::from_utf8_lossy(output)
            );
            return;
        }
        let locked = serde_json::from_slice::<serde_json::Value>(&stdout)
            .ok()
            .and_then(|value| value.get("locked").and_then(|value| value.as_bool()));
        let Some(locked) = locked else {
            self.session_status = "Invalid session-lock response".to_owned();
            return;
        };
        self.session_locked = Some(locked);
        match operation.as_str() {
            "query" if locked => reconfigure("on_force_close \"detach\"".to_owned(), false),
            "query" => reconfigure("on_force_close \"quit\"".to_owned(), false),
            "mark" if locked => {
                reconfigure("on_force_close \"detach\"".to_owned(), false);
                self.session_status = "Locked — detaches on terminal close".to_owned();
                if self.session_role {
                    close_focus();
                }
            }
            "unmark" if !locked => {
                reconfigure("on_force_close \"quit\"".to_owned(), false);
                self.session_status = "Unlocked — quits on terminal close".to_owned();
                if self.session_role {
                    close_focus();
                }
            }
            _ => {}
        }
    }

    fn handle_session_key(&mut self, key: KeyWithModifier) -> bool {
        match key.bare_key {
            BareKey::Esc => {
                close_focus();
                return false;
            }
            BareKey::Backspace if self.session_locked == Some(false) => {
                self.session_input.pop();
            }
            BareKey::Enter => {
                if self.session_locked.is_none() {
                    self.query_session_lock();
                    self.session_status = "Retrying session lock query…".to_owned();
                } else if self.session_locked == Some(true) {
                    if let Some(name) = self.session_name.clone() {
                        self.session_command("unmark", &name);
                    }
                } else if self.session_input.chars().all(|character| {
                    character.is_ascii_alphanumeric() || " ._-".contains(character)
                }) && !self.session_input.is_empty()
                    && self.session_input.chars().count() <= 64
                {
                    if self.session_name.as_deref() == Some(self.session_input.as_str()) {
                        self.session_command("mark", &self.session_input.clone());
                        self.session_status = "Locking session…".to_owned();
                    } else {
                        rename_session(&self.session_input);
                        self.pending_session_name = Some(self.session_input.clone());
                        self.session_status = "Renaming session…".to_owned();
                    }
                } else {
                    self.session_status = "Use 1–64 letters, digits, spaces, ., _, or -".to_owned();
                }
            }
            BareKey::Char(character)
                if self.session_locked == Some(false) && key.key_modifiers.is_empty() =>
            {
                self.session_input.push(character)
            }
            _ => {}
        }
        true
    }

    fn reconcile_tabs(&mut self) {
        let active_tab_ids: Vec<usize> = self.tabs.iter().map(|tab| tab.tab_id).collect();
        let retain_actions = self
            .adapter
            .as_mut()
            .map(|adapter| adapter.retain_tabs(&active_tab_ids))
            .unwrap_or_default();
        self.execute(retain_actions);

        let max_chars = self.adapter.as_ref().map(Adapter::max_chars).unwrap_or(32);
        let work: Vec<_> = self
            .tabs
            .iter()
            .filter_map(|tab| {
                let snapshots = self.snapshots_for_tab(tab);
                let pane = choose_pane(&snapshots)?;
                Some((
                    tab.tab_id,
                    tab.name.clone(),
                    canonical_source_for_pane(pane),
                    label_for_pane(pane, max_chars),
                ))
            })
            .collect();

        for (tab_id, current_name, source, fallback) in work {
            let actions = self
                .adapter
                .as_mut()
                .map(|adapter| adapter.reconcile(tab_id, &current_name, &source, &fallback))
                .unwrap_or_default();
            self.execute(actions);
        }
    }

    fn snapshots_for_tab(&self, tab: &TabInfo) -> Vec<PaneSnapshot> {
        self.panes
            .panes
            .get(&tab.position)
            .into_iter()
            .flatten()
            .filter(|pane| !pane.is_plugin && !pane.exited && !pane.is_suppressed)
            .map(|pane| {
                let pane_id = PaneId::Terminal(pane.id);
                let command = pane.terminal_command.clone().unwrap_or_else(|| {
                    get_pane_running_command(pane_id)
                        .map(|parts| parts.join(" "))
                        .unwrap_or_default()
                });
                let cwd = get_pane_cwd(pane_id)
                    .map(|path| path.to_string_lossy().into_owned())
                    .unwrap_or_default();
                PaneSnapshot {
                    title: pane.title.clone(),
                    command,
                    cwd,
                    focused: pane.is_focused,
                    floating: pane.is_floating,
                }
            })
            .collect()
    }

    fn execute(&self, actions: Vec<Action>) {
        for action in actions {
            match action {
                Action::RequestPermissions { web_access } => {
                    let mut permissions = vec![
                        PermissionType::ReadApplicationState,
                        PermissionType::ChangeApplicationState,
                        PermissionType::RunCommands,
                        PermissionType::Reconfigure,
                        PermissionType::OpenTerminalsOrPlugins,
                    ];
                    if web_access {
                        permissions.push(PermissionType::WebAccess);
                    }
                    request_permission(&permissions);
                }
                Action::Subscribe { web_results } => {
                    let mut events = vec![
                        EventType::TabUpdate,
                        EventType::PaneUpdate,
                        EventType::ModeUpdate,
                        EventType::RunCommandResult,
                        EventType::Timer,
                        EventType::CwdChanged,
                        EventType::CommandChanged,
                    ];
                    if self.session_role {
                        events.push(EventType::Key);
                        events.push(EventType::PastedText);
                    }
                    if web_results {
                        events.push(EventType::WebRequestResult);
                    }
                    subscribe(&events);
                }
                Action::Rename { tab_id, label } => rename_tab_with_id(tab_id as u64, &label),
                Action::WebRequest {
                    url,
                    headers,
                    body,
                    context,
                } => web_request(url, HttpVerb::Post, headers, body, context),
                Action::Diagnostic { category } => eprintln!("zellij-tab-namer: {category:?}"),
            }
        }
    }
}
