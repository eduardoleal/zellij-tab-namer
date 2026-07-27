#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LifecycleAction {
    None,
    RefreshLockAndScheduleQuitCheck,
    ScheduleQuitCheck,
    Quit,
}

#[derive(Debug, Default)]
pub struct EphemeralLifecycle {
    seen_attached: bool,
    connected_clients: usize,
    quit_check_pending: bool,
}

impl EphemeralLifecycle {
    pub fn observe_clients(&mut self, connected_clients: usize) -> LifecycleAction {
        if connected_clients == self.connected_clients {
            return LifecycleAction::None;
        }
        self.connected_clients = connected_clients;
        if connected_clients > 0 {
            self.seen_attached = true;
            self.quit_check_pending = false;
            return LifecycleAction::None;
        }
        // The lock role and the headless role are separate plugin instances.
        // Always recheck durable lock state when the final client leaves rather
        // than trusting the headless instance's cached value.
        if self.seen_attached && !self.quit_check_pending {
            self.quit_check_pending = true;
            return LifecycleAction::RefreshLockAndScheduleQuitCheck;
        }
        LifecycleAction::None
    }

    pub fn lock_state_changed(&mut self, locked: Option<bool>) -> LifecycleAction {
        if locked == Some(false)
            && self.seen_attached
            && self.connected_clients == 0
            && !self.quit_check_pending
        {
            self.quit_check_pending = true;
            return LifecycleAction::ScheduleQuitCheck;
        }
        LifecycleAction::None
    }

    pub fn confirm_quit(&mut self, locked: Option<bool>) -> LifecycleAction {
        if !self.quit_check_pending {
            return LifecycleAction::None;
        }
        self.quit_check_pending = false;
        if self.seen_attached && self.connected_clients == 0 && locked == Some(false) {
            LifecycleAction::Quit
        } else {
            LifecycleAction::None
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SwitchGuardAction {
    Wait,
    Warn,
    OpenSessionManager,
}

pub fn switch_guard_action(locked: Option<bool>) -> SwitchGuardAction {
    match locked {
        Some(true) => SwitchGuardAction::OpenSessionManager,
        Some(false) => SwitchGuardAction::Warn,
        None => SwitchGuardAction::Wait,
    }
}

pub fn switch_guard_enter_opens_manager(locked: Option<bool>) -> bool {
    // Unknown state fails safe in the headless lifecycle, so the guard must
    // not make the built-in session manager unreachable when lookup fails.
    locked != Some(true)
}

pub fn timer_matches(actual: f64, expected: f64) -> bool {
    (actual - expected).abs() < f64::EPSILON
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unlocked_session_quits_after_last_client_leaves_and_check_is_confirmed() {
        let mut lifecycle = EphemeralLifecycle::default();

        assert_eq!(lifecycle.observe_clients(1), LifecycleAction::None);
        assert_eq!(
            lifecycle.observe_clients(0),
            LifecycleAction::RefreshLockAndScheduleQuitCheck
        );
        assert_eq!(lifecycle.confirm_quit(Some(false)), LifecycleAction::Quit);
    }

    #[test]
    fn locked_session_rechecks_then_survives_last_client_leaving() {
        let mut lifecycle = EphemeralLifecycle::default();

        lifecycle.observe_clients(1);
        assert_eq!(
            lifecycle.observe_clients(0),
            LifecycleAction::RefreshLockAndScheduleQuitCheck
        );
        assert_eq!(lifecycle.confirm_quit(Some(true)), LifecycleAction::None);
    }

    #[test]
    fn startup_without_an_attached_client_never_quits() {
        let mut lifecycle = EphemeralLifecycle::default();

        assert_eq!(lifecycle.observe_clients(0), LifecycleAction::None);
        assert_eq!(lifecycle.confirm_quit(Some(false)), LifecycleAction::None);
    }

    #[test]
    fn another_client_joining_cancels_pending_quit() {
        let mut lifecycle = EphemeralLifecycle::default();

        lifecycle.observe_clients(1);
        lifecycle.observe_clients(0);
        lifecycle.observe_clients(1);

        assert_eq!(lifecycle.confirm_quit(Some(false)), LifecycleAction::None);
    }

    #[test]
    fn late_unlocked_state_schedules_a_check_for_an_empty_session() {
        let mut lifecycle = EphemeralLifecycle::default();

        lifecycle.observe_clients(1);
        assert_eq!(
            lifecycle.observe_clients(0),
            LifecycleAction::RefreshLockAndScheduleQuitCheck
        );
        assert_eq!(lifecycle.confirm_quit(None), LifecycleAction::None);
        assert_eq!(
            lifecycle.lock_state_changed(Some(false)),
            LifecycleAction::ScheduleQuitCheck
        );
    }

    #[test]
    fn multiple_clients_only_quit_after_the_last_one_leaves() {
        let mut lifecycle = EphemeralLifecycle::default();

        lifecycle.observe_clients(2);
        assert_eq!(lifecycle.observe_clients(1), LifecycleAction::None);
        assert_eq!(
            lifecycle.observe_clients(0),
            LifecycleAction::RefreshLockAndScheduleQuitCheck
        );
    }

    #[test]
    fn repeated_client_counts_do_not_reschedule_work() {
        let mut lifecycle = EphemeralLifecycle::default();

        lifecycle.observe_clients(1);
        assert_eq!(lifecycle.observe_clients(1), LifecycleAction::None);
        assert_eq!(
            lifecycle.observe_clients(0),
            LifecycleAction::RefreshLockAndScheduleQuitCheck
        );
        assert_eq!(lifecycle.observe_clients(0), LifecycleAction::None);
    }

    #[test]
    fn switch_guard_warns_only_for_confirmed_unlocked_sessions() {
        assert_eq!(switch_guard_action(Some(false)), SwitchGuardAction::Warn);
        assert_eq!(
            switch_guard_action(Some(true)),
            SwitchGuardAction::OpenSessionManager
        );
        assert_eq!(switch_guard_action(None), SwitchGuardAction::Wait);
    }

    #[test]
    fn switch_guard_enter_fails_open_when_lock_state_is_unknown() {
        assert!(switch_guard_enter_opens_manager(Some(false)));
        assert!(switch_guard_enter_opens_manager(None));
        assert!(!switch_guard_enter_opens_manager(Some(true)));
    }

    #[test]
    fn timer_matching_keeps_lifecycle_and_naming_events_separate() {
        assert!(timer_matches(0.5, 0.5));
        assert!(!timer_matches(0.35, 0.5));
        assert!(!timer_matches(3.0, 0.5));
    }
}
