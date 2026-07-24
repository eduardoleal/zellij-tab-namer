use std::collections::{BTreeMap, HashMap, HashSet};

use crate::llm::{build_chat_request, chat_completions_url, OllamaConfig, ResponseError};
use crate::naming::{RefinementCoordinator, RenameDecision, ScheduledRefinement};

const DEFAULT_MAX_CHARS: usize = 32;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum DiagnosticCategory {
    IncompleteConfiguration,
    PermissionDenied,
    TransportFailure,
    HttpStatus,
    ValidationRejected,
    SchedulerSaturated,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Action {
    RequestPermissions {
        web_access: bool,
    },
    Subscribe {
        web_results: bool,
    },
    Rename {
        tab_id: usize,
        label: String,
    },
    WebRequest {
        url: String,
        headers: BTreeMap<String, String>,
        body: Vec<u8>,
        context: BTreeMap<String, String>,
    },
    Diagnostic {
        category: DiagnosticCategory,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum WebPermission {
    Disabled,
    Granted,
    Denied,
}

#[derive(Clone, Debug)]
pub struct NativeConfig {
    pub max_chars: usize,
    pub ollama: Option<OllamaConfig>,
    incomplete_llm: bool,
    session_lock_command: String,
    session_lock_state_file: String,
}

impl NativeConfig {
    pub fn parse(configuration: &BTreeMap<String, String>) -> Self {
        let max_chars = configuration
            .get("max_chars")
            .and_then(|value| value.parse().ok())
            .filter(|value| (8..=120).contains(value))
            .unwrap_or(DEFAULT_MAX_CHARS);
        let has_base_url = configuration.contains_key("llm_base_url");
        let has_model = configuration.contains_key("llm_model");
        let base_url = configuration
            .get("llm_base_url")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty());
        let model = configuration
            .get("llm_model")
            .map(|value| value.trim())
            .filter(|value| !value.is_empty());
        let incomplete_llm = (has_base_url || has_model)
            && (base_url.is_none()
                || model.is_none()
                || base_url.is_some_and(|value| chat_completions_url(value).is_err()));
        let ollama = match (base_url, model) {
            (Some(base_url), Some(model)) if !incomplete_llm => Some(OllamaConfig {
                base_url: base_url.to_owned(),
                model: model.to_owned(),
            }),
            _ => None,
        };
        Self {
            max_chars,
            ollama,
            incomplete_llm,
            session_lock_command: configuration
                .get("session_lock_command")
                .cloned()
                .unwrap_or_else(|| "zellij-tab-namer".to_owned()),
            session_lock_state_file: configuration
                .get("session_lock_state_file")
                .cloned()
                .unwrap_or_default(),
        }
    }
}

pub struct Adapter {
    config: NativeConfig,
    permission: WebPermission,
    coordinator: RefinementCoordinator,
    diagnostics: HashMap<usize, (u64, HashSet<DiagnosticCategory>)>,
}

impl Adapter {
    pub fn new(configuration: BTreeMap<String, String>) -> Self {
        let config = NativeConfig::parse(&configuration);
        let permission = if config.ollama.is_some() {
            // Native configuration is installed together with a path-keyed
            // WebAccess pre-grant. Zellij does not emit a permission result
            // when that cached grant is already satisfied, so configuration
            // is the readiness assertion; a later denial still closes it.
            WebPermission::Granted
        } else {
            WebPermission::Disabled
        };
        Self {
            config,
            permission,
            coordinator: RefinementCoordinator::default(),
            diagnostics: HashMap::new(),
        }
    }

    pub fn max_chars(&self) -> usize {
        self.config.max_chars
    }

    pub fn session_lock_command(&self) -> String {
        self.config.session_lock_command.clone()
    }

    pub fn session_lock_state_file(&self) -> String {
        self.config.session_lock_state_file.clone()
    }

    pub fn load_actions(&self) -> Vec<Action> {
        let web_access = self.config.ollama.is_some();
        vec![
            Action::RequestPermissions { web_access },
            Action::Subscribe {
                web_results: web_access,
            },
        ]
    }

    pub fn permission_result(&mut self, granted: bool) {
        if self.config.ollama.is_some() {
            self.permission = if granted {
                WebPermission::Granted
            } else {
                WebPermission::Denied
            };
        }
    }

    pub fn reconcile(
        &mut self,
        tab_id: usize,
        current_name: &str,
        source: &str,
        fallback: &str,
    ) -> Vec<Action> {
        let enabled = self.permission == WebPermission::Granted;
        let decision = self.coordinator.reconcile(
            tab_id,
            current_name,
            source,
            fallback,
            self.config.max_chars,
            enabled,
        );
        let mut actions = Vec::new();
        if let RenameDecision::Rename(label) = decision.rename {
            actions.push(Action::Rename { tab_id, label });
        }
        if self.config.incomplete_llm {
            self.diagnostic_once(
                tab_id,
                decision.generation,
                DiagnosticCategory::IncompleteConfiguration,
                &mut actions,
            );
        } else if self.permission == WebPermission::Denied {
            self.diagnostic_once(
                tab_id,
                decision.generation,
                DiagnosticCategory::PermissionDenied,
                &mut actions,
            );
        }
        if enabled
            && source.chars().count() > self.config.max_chars
            && decision.requests.is_empty()
            && self.coordinator.in_flight_count() >= 2
        {
            self.diagnostic_once(
                tab_id,
                decision.generation,
                DiagnosticCategory::SchedulerSaturated,
                &mut actions,
            );
        }
        self.append_requests(decision.requests, &mut actions);
        actions
    }

    pub fn retain_tabs(&mut self, active_tab_ids: &[usize]) -> Vec<Action> {
        let requests = self.coordinator.retain_tabs(active_tab_ids);
        self.diagnostics
            .retain(|tab_id, _| active_tab_ids.contains(tab_id));
        let mut actions = Vec::new();
        self.append_requests(requests, &mut actions);
        actions
    }

    pub fn web_result(
        &mut self,
        status: u16,
        body: &[u8],
        context: &BTreeMap<String, String>,
        live_tab_names: &BTreeMap<usize, String>,
    ) -> Vec<Action> {
        let Some(request_id) = context
            .get("request_id")
            .and_then(|request_id| request_id.parse::<u64>().ok())
        else {
            return Vec::new();
        };
        let mut actions = Vec::new();
        let completion = self
            .coordinator
            .finish(request_id, status, body, live_tab_names);
        if let Some(error) = completion
            .response_error
            .as_ref()
            .filter(|_| completion.recognized)
        {
            let category = match error {
                ResponseError::HttpStatus if status == 0 => DiagnosticCategory::TransportFailure,
                ResponseError::HttpStatus => DiagnosticCategory::HttpStatus,
                _ => DiagnosticCategory::ValidationRejected,
            };
            actions.push(Action::Diagnostic { category });
        }
        if let Some((tab_id, label)) = completion.rename {
            actions.push(Action::Rename { tab_id, label });
        }
        self.append_requests(completion.requests, &mut actions);
        actions
    }

    fn append_requests(&mut self, requests: Vec<ScheduledRefinement>, actions: &mut Vec<Action>) {
        let Some(config) = self.config.ollama.clone() else {
            return;
        };
        for request in requests {
            match build_chat_request(&config, &request.source, request.max_chars) {
                Ok(chat) => {
                    actions.push(Action::WebRequest {
                        url: chat.url,
                        headers: BTreeMap::from([(
                            "content-type".to_owned(),
                            "application/json".to_owned(),
                        )]),
                        body: chat.body,
                        context: BTreeMap::from([(
                            "request_id".to_owned(),
                            request.request_id.to_string(),
                        )]),
                    });
                }
                Err(_) => {
                    actions.push(Action::Diagnostic {
                        category: DiagnosticCategory::ValidationRejected,
                    });
                    let completion =
                        self.coordinator
                            .finish(request.request_id, 500, b"", &BTreeMap::new());
                    if let Some((tab_id, label)) = completion.rename {
                        actions.push(Action::Rename { tab_id, label });
                    }
                    self.append_requests(completion.requests, actions);
                }
            }
        }
    }

    fn diagnostic_once(
        &mut self,
        tab_id: usize,
        generation: u64,
        category: DiagnosticCategory,
        actions: &mut Vec<Action>,
    ) {
        let entry = self
            .diagnostics
            .entry(tab_id)
            .or_insert_with(|| (generation, HashSet::new()));
        if entry.0 != generation {
            *entry = (generation, HashSet::new());
        }
        if entry.1.insert(category.clone()) {
            actions.push(Action::Diagnostic { category });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn configured() -> BTreeMap<String, String> {
        BTreeMap::from([
            ("max_chars".into(), "18".into()),
            ("llm_base_url".into(), "http://localhost:11434/v1".into()),
            ("llm_model".into(), "llama3.2".into()),
        ])
    }

    #[test]
    fn load_contract_is_conditional_on_complete_llm_configuration() {
        let local = Adapter::new(BTreeMap::new());
        assert_eq!(
            local.load_actions(),
            vec![
                Action::RequestPermissions { web_access: false },
                Action::Subscribe { web_results: false },
            ]
        );

        let enabled = Adapter::new(configured());
        assert_eq!(
            enabled.load_actions(),
            vec![
                Action::RequestPermissions { web_access: true },
                Action::Subscribe { web_results: true },
            ]
        );
    }

    #[test]
    fn fallback_must_be_observed_before_async_request() {
        let mut adapter = Adapter::new(configured());
        let actions = adapter.reconcile(
            3,
            "Tab #4",
            "implement native ollama compression safely",
            "implement native...",
        );
        assert!(matches!(actions[0], Action::Rename { tab_id: 3, .. }));
        assert!(actions
            .iter()
            .all(|action| !matches!(action, Action::WebRequest { .. })));
        let observed = adapter.reconcile(
            3,
            "implement native...",
            "implement native ollama compression safely",
            "implement native...",
        );
        assert!(matches!(observed[0], Action::WebRequest { .. }));
    }

    #[test]
    fn denied_permission_does_not_consume_generation() {
        let mut adapter = Adapter::new(configured());
        adapter.permission_result(false);
        let denied = adapter.reconcile(
            4,
            "Tab #5",
            "a sufficiently long source title",
            "long source...",
        );
        assert!(denied
            .iter()
            .all(|action| !matches!(action, Action::WebRequest { .. })));
        assert!(denied.contains(&Action::Diagnostic {
            category: DiagnosticCategory::PermissionDenied,
        }));

        adapter.permission_result(true);
        assert!(adapter
            .reconcile(
                4,
                "long source...",
                "a sufficiently long source title",
                "long source..."
            )
            .iter()
            .any(|action| matches!(action, Action::WebRequest { .. })));
    }

    #[test]
    fn incomplete_configuration_diagnostic_is_generation_bounded() {
        let mut adapter = Adapter::new(BTreeMap::from([(
            "llm_base_url".into(),
            "http://localhost:11434/v1".into(),
        )]));
        let first = adapter.reconcile(
            6,
            "Tab #7",
            "a sufficiently long source title",
            "long source...",
        );
        assert!(first.contains(&Action::Diagnostic {
            category: DiagnosticCategory::IncompleteConfiguration,
        }));
        let repeated = adapter.reconcile(
            6,
            "long source...",
            "a sufficiently long source title",
            "long source...",
        );
        assert!(repeated
            .iter()
            .all(|action| !matches!(action, Action::Diagnostic { .. })));
    }

    #[test]
    fn request_contains_only_json_contract_and_opaque_id_context() {
        let mut adapter = Adapter::new(configured());
        adapter.reconcile(
            9,
            "Tab #10",
            "review native integration metadata carefully",
            "review native...",
        );
        let request = adapter
            .reconcile(
                9,
                "review native...",
                "review native integration metadata carefully",
                "review native...",
            )
            .into_iter()
            .find_map(|action| match action {
                Action::WebRequest {
                    headers,
                    body,
                    context,
                    ..
                } => Some((headers, body, context)),
                _ => None,
            })
            .unwrap();
        assert_eq!(request.0.get("content-type").unwrap(), "application/json");
        assert_eq!(request.2.len(), 1);
        assert!(request.2.contains_key("request_id"));
        let serialized = String::from_utf8(request.1).unwrap();
        assert!(!serialized.contains("tab_id"));
        assert!(!serialized.contains("cwd"));
    }

    #[test]
    fn result_does_not_touch_debounce_and_diagnostics_are_generation_bounded() {
        let mut adapter = Adapter::new(configured());
        adapter.permission_result(true);
        adapter.reconcile(
            5,
            "Tab #6",
            "debug asynchronous web result routing",
            "debug async...",
        );
        let actions = adapter.reconcile(
            5,
            "debug async...",
            "debug asynchronous web result routing",
            "debug async...",
        );
        let context = actions
            .iter()
            .find_map(|action| match action {
                Action::WebRequest { context, .. } => Some(context.clone()),
                _ => None,
            })
            .unwrap();
        let live_names = BTreeMap::from([(5, "debug async...".to_owned())]);
        let first = adapter.web_result(503, b"unavailable", &context, &live_names);
        assert_eq!(
            first,
            vec![Action::Diagnostic {
                category: DiagnosticCategory::HttpStatus,
            }]
        );
        assert!(adapter
            .web_result(503, b"unavailable", &context, &live_names)
            .is_empty());
    }
}
