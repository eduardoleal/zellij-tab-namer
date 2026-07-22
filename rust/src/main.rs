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
}

register_plugin!(TabNamer);

impl ZellijPlugin for TabNamer {
    fn load(&mut self, configuration: BTreeMap<String, String>) {
        let adapter = Adapter::new(configuration);
        self.execute(adapter.load_actions());
        self.adapter = Some(adapter);
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
            Event::PermissionRequestResult(status) => {
                if let Some(adapter) = &mut self.adapter {
                    adapter.permission_result(status == PermissionStatus::Granted);
                }
                self.schedule_update();
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
                        EventType::Timer,
                        EventType::CwdChanged,
                        EventType::CommandChanged,
                    ];
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
