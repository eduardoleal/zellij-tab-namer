use std::collections::BTreeMap;

use zellij_tab_namer_plugin::naming::{choose_pane, NamingState, PaneSnapshot, RenameDecision};
use zellij_tile::prelude::*;

const DEFAULT_MAX_CHARS: usize = 32;
const DEBOUNCE_SECONDS: f64 = 0.35;

struct TabNamer {
    max_chars: usize,
    tabs: Vec<TabInfo>,
    panes: PaneManifest,
    naming: NamingState,
    update_pending: bool,
}

impl Default for TabNamer {
    fn default() -> Self {
        Self {
            max_chars: DEFAULT_MAX_CHARS,
            tabs: Vec::new(),
            panes: PaneManifest::default(),
            naming: NamingState::default(),
            update_pending: false,
        }
    }
}

register_plugin!(TabNamer);

impl ZellijPlugin for TabNamer {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        self.max_chars = configuration
            .get("max_chars")
            .and_then(|value| value.parse().ok())
            .filter(|value| (8..=120).contains(value))
            .unwrap_or(DEFAULT_MAX_CHARS);

        request_permission(&[
            PermissionType::ReadApplicationState,
            PermissionType::ChangeApplicationState,
        ]);
        subscribe(&[
            EventType::TabUpdate,
            EventType::PaneUpdate,
            EventType::Timer,
            EventType::CwdChanged,
            EventType::CommandChanged,
        ]);
    }

    fn update(&mut self, event: Event) -> bool {
        match event {
            Event::TabUpdate(tabs) => {
                self.tabs = tabs;
                self.schedule_update();
            }
            Event::PaneUpdate(panes) => {
                self.panes = panes;
                self.schedule_update();
            }
            Event::CwdChanged(..) | Event::CommandChanged(..) => self.schedule_update(),
            Event::Timer(_) => {
                self.update_pending = false;
                self.reconcile_tabs();
            }
            _ => {}
        }
        false
    }
}

impl TabNamer {
    fn schedule_update(&mut self) {
        if !self.update_pending {
            self.update_pending = true;
            set_timeout(DEBOUNCE_SECONDS);
        }
    }

    fn reconcile_tabs(&mut self) {
        let active_tab_ids: Vec<usize> = self.tabs.iter().map(|tab| tab.tab_id).collect();
        self.naming.retain_tabs(&active_tab_ids);

        for tab in &self.tabs {
            let snapshots = self.snapshots_for_tab(tab);
            let Some(pane) = choose_pane(&snapshots) else {
                continue;
            };

            if let RenameDecision::Rename(candidate) =
                self.naming
                    .decision(tab.tab_id, &tab.name, pane, self.max_chars)
            {
                rename_tab_with_id(tab.tab_id as u64, candidate);
            }
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
}
