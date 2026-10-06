package winsettings

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// This fixture separates keys, and models the restricted helper's intent /
// expected-value contract. Native tests exercise the embedded script itself
// using disposable keys; these tests never run PowerShell or request UAC.
type webRTCRegistry struct {
	values    map[string]map[string]Value
	readError error
	fail      func(key, name string, value Value) error
	writes    []string
}

func newWebRTCRegistry() *webRTCRegistry {
	return &webRTCRegistry{values: map[string]map[string]Value{}}
}

func (r *webRTCRegistry) Read(key string, names []string) (map[string]Value, error) {
	if r.readError != nil {
		return nil, r.readError
	}
	result := map[string]Value{}
	for _, name := range names {
		if value, ok := r.values[key][name]; ok {
			result[name] = value
		}
	}
	return result, nil
}

func (r *webRTCRegistry) put(key, name string, value Value) {
	if r.values[key] == nil {
		r.values[key] = map[string]Value{}
	}
	if value.Kind == "" {
		delete(r.values[key], name)
	} else {
		r.values[key][name] = value
	}
}

func (r *webRTCRegistry) Apply(key string, changes map[string]Value, _ string) error {
	for name, value := range changes {
		if r.fail != nil {
			if err := r.fail(key, name, value); err != nil {
				return err
			}
		}
		r.writes = append(r.writes, key+`\`+name)
		r.put(key, name, value)
	}
	return nil
}

func (r *webRTCRegistry) change(action string) error {
	if action == "on" {
		for _, policy := range webRTCPolicies {
			value := r.values[policy.key][policy.name]
			if policy.chromium && value.Kind != "" && value != policy.want {
				return errors.New("fixture: conflict")
			}
		}
	}
	for _, policy := range webRTCPolicies {
		value := r.values[policy.key][policy.name]
		if action == "on" && !policy.obsolete && value.Kind == "" {
			if err := r.Apply(WebRTCMetadataKey, map[string]Value{policy.owner: {WebRTCMetadataIntent, WebRTCMetadataKind}}, ""); err != nil {
				return err
			}
			if err := r.Apply(policy.key, map[string]Value{policy.name: policy.want}, ""); err != nil {
				return err
			}
		} else if (action == "off" || policy.obsolete) && r.values[WebRTCMetadataKey][policy.owner].Kind != "" {
			if value == policy.want {
				if err := r.Apply(policy.key, map[string]Value{policy.name: {}}, ""); err != nil {
					return err
				}
			}
			if err := r.Apply(WebRTCMetadataKey, map[string]Value{policy.owner: {}}, ""); err != nil {
				return err
			}
		}
	}
	return nil
}

func webRTCFixture(t *testing.T) (*WebRTC, *webRTCRegistry, *[]string) {
	t.Helper()
	registry := newWebRTCRegistry()
	var changes []string
	w := &WebRTC{
		Registry:  registry,
		StateFile: filepath.Join(t.TempDir(), "state", "webrtc-mode"),
		Change: func(action string) error {
			changes = append(changes, action)
			return registry.change(action)
		},
	}
	return w, registry, &changes
}

func assertOffMarker(t *testing.T, w *WebRTC, present bool) {
	t.Helper()
	data, err := os.ReadFile(w.StateFile)
	if present {
		if err != nil || string(data) != "off\n" {
			t.Fatalf("off marker: %q %v", data, err)
		}
	} else if !os.IsNotExist(err) {
		t.Fatalf("state should be absent: %q %v", data, err)
	}
}

func TestWebRTCEnsureDefaultsOnVerifiesAndRepairsWithoutRepeatedApproval(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	profiles := 0
	w.ApplyProfiles = func() error { profiles++; return nil }
	for range 3 {
		if err := w.Ensure(); err != nil {
			t.Fatal(err)
		}
	}
	if !reflect.DeepEqual(*changes, []string{"on"}) || profiles != 3 {
		t.Fatalf("ordinary toggles prompted again: %v profiles=%d", *changes, profiles)
	}
	// Losing a policy while the proxy is on must trigger verified repair.
	registry.put(ChromePolicyKey, WebRtcPolicyName, Value{})
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	if len(*changes) != 2 || registry.values[ChromePolicyKey][WebRtcPolicyName] != webRTCPolicies[0].want {
		t.Fatal("missing policy was not repaired")
	}
	assertOffMarker(t, w, false)
}

func TestWebRTCExplicitOffPersistsThroughCancellationAndAutomaticSetup(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	w.Change = func(action string) error {
		*changes = append(*changes, action)
		return context.Canceled
	}
	if err := w.Set(false); !errors.Is(err, context.Canceled) {
		t.Fatalf("lost cancellation: %v", err)
	}
	assertOffMarker(t, w, true)
	registry.readError = errors.New("automatic setup must not even read registry")
	w.ApplyProfiles = func() error { t.Fatal("opt-out applied profiles"); return nil }
	for range 3 {
		if err := w.Ensure(); err != nil {
			t.Fatal(err)
		}
	}
	if len(*changes) != 2 {
		t.Fatal("opt-out requested approval")
	}
	registry.readError = nil
	w.Change = registry.change
	if err := w.Cleanup(); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, w, true)
}

func TestWebRTCMatchingPreexistingAndFirefoxForeignValuesAreNotOwned(t *testing.T) {
	w, registry, _ := webRTCFixture(t)
	manualChrome := webRTCPolicies[0].want
	manualFirefox := Value{"false", "String"}
	registry.put(ChromePolicyKey, WebRtcPolicyName, manualChrome)
	registry.put(FirefoxPolicyKey, FirefoxProxyOnlyName, manualFirefox)
	registry.put(ChromePolicyKey, "Unrelated", Value{"private fixture text", "String"})
	registry.put(WebRTCMetadataKey, "Unrelated", Value{"private fixture text", "String"})
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	if registry.values[WebRTCMetadataKey][WebRTCChromeOwnerName].Kind != "" || registry.values[WebRTCMetadataKey][WebRTCFirefoxProxyName].Kind != "" {
		t.Fatal("pre-existing policies were claimed")
	}
	// Later edits, including changes to type alone, must also survive off.
	registry.put(EdgePolicyKey, EdgeWebRtcPolicyName, Value{"private external edit", "String"})
	registry.put(BravePolicyKey, WebRtcPolicyName, Value{WebRtcDisableNonProxiedUDP, "ExpandString"})
	if err := w.Set(false); err != nil {
		t.Fatal(err)
	}
	if registry.values[ChromePolicyKey][WebRtcPolicyName] != manualChrome || registry.values[FirefoxPolicyKey][FirefoxProxyOnlyName] != manualFirefox {
		t.Fatal("reset removed pre-existing values")
	}
	if registry.values[EdgePolicyKey][EdgeWebRtcPolicyName].Text != "private external edit" || registry.values[BravePolicyKey][WebRtcPolicyName].Kind != "ExpandString" {
		t.Fatal("reset removed external edits")
	}
	for _, policy := range webRTCPolicies {
		if registry.values[WebRTCMetadataKey][policy.owner].Kind != "" {
			t.Fatal("cleanup left owned metadata")
		}
	}
	if registry.values[ChromePolicyKey]["Unrelated"].Kind == "" || registry.values[WebRTCMetadataKey]["Unrelated"].Kind == "" {
		t.Fatal("cleanup touched unrelated values")
	}
	locations, err := w.Remaining()
	if err != nil || len(locations) != 4 {
		t.Fatalf("remaining locations: %v %v", locations, err)
	}
	for _, location := range locations {
		if !strings.HasPrefix(location, `HKCU\Software\Policies\`) || strings.Contains(location, "private") || strings.Contains(location, WebRtcDisableNonProxiedUDP) {
			t.Fatalf("location disclosed a value: %s", location)
		}
	}
}

func TestWebRTCConflictsAndReadFailuresNeverRequestApproval(t *testing.T) {
	for _, foreign := range []Value{{"default_public_interface_only", "String"}, {WebRtcDisableNonProxiedUDP, "ExpandString"}, {"", "String"}} {
		w, registry, changes := webRTCFixture(t)
		registry.put(BravePolicyKey, WebRtcPolicyName, foreign)
		if err := w.Ensure(); err == nil || strings.Contains(err.Error(), foreign.Text) && foreign.Text != "" {
			t.Fatalf("conflict was not safe: %v", err)
		}
		if len(*changes) != 0 || len(registry.writes) != 0 {
			t.Fatal("conflict caused partial setup or approval")
		}
	}
	w, registry, changes := webRTCFixture(t)
	failure := errors.New("fixture: read failed")
	registry.readError = failure
	if err := w.Ensure(); !errors.Is(err, failure) {
		t.Fatalf("lost read failure: %v", err)
	}
	if _, err := w.Remaining(); !errors.Is(err, failure) {
		t.Fatalf("lost Remaining failure: %v", err)
	}
	if err := w.Cleanup(); !errors.Is(err, failure) || len(*changes) != 0 {
		t.Fatalf("cleanup: %v changes=%v", err, *changes)
	}
}

func TestWebRTCPartialSetupDurableIntentAndExplicitOnRetry(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Set(false); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("fixture: policy write denied")
	registry.fail = func(key, _ string, _ Value) error {
		if key == EdgePolicyKey {
			return failure
		}
		return nil
	}
	profiles := 0
	w.ApplyProfiles = func() error { profiles++; return nil }
	if err := w.Set(true); !errors.Is(err, failure) {
		t.Fatalf("lost setup failure: %v", err)
	}
	assertOffMarker(t, w, true)
	if registry.values[WebRTCMetadataKey][WebRTCEdgeOwnerName] != (Value{WebRTCMetadataIntent, WebRTCMetadataKind}) || registry.values[EdgePolicyKey][EdgeWebRtcPolicyName].Kind != "" {
		t.Fatal("failed write did not retain intent")
	}
	if registry.values[ChromePolicyKey][WebRtcPolicyName] != webRTCPolicies[0].want || profiles != 0 {
		t.Fatal("partial setup or profile sequencing is wrong")
	}
	// A new process can retry using only the protected registry intents.
	registry.fail = nil
	restarted := &WebRTC{Registry: registry, StateFile: w.StateFile, Change: w.Change, ApplyProfiles: w.ApplyProfiles}
	if err := restarted.Ensure(); err != nil || len(*changes) != 1 {
		t.Fatalf("automatic opt-out retry: %v %v", err, *changes)
	}
	if err := restarted.Set(true); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, restarted, false)
	if len(*changes) != 2 || profiles != 1 {
		t.Fatal("explicit retry did not finish setup")
	}
}

func TestWebRTCCleanupFailureLeavesIntentAndModeForRetry(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("fixture: delete denied")
	registry.fail = func(key, _ string, value Value) error {
		if key == EdgePolicyKey && value.Kind == "" {
			return failure
		}
		return nil
	}
	reverts := 0
	w.RevertProfiles = func() error { reverts++; return nil }
	if err := w.Set(false); !errors.Is(err, failure) {
		t.Fatalf("lost cleanup failure: %v", err)
	}
	assertOffMarker(t, w, true)
	if registry.values[ChromePolicyKey][WebRtcPolicyName].Kind != "" || registry.values[WebRTCMetadataKey][WebRTCChromeOwnerName].Kind != "" {
		t.Fatal("completed cleanup was not recorded")
	}
	if registry.values[WebRTCMetadataKey][WebRTCEdgeOwnerName].Kind == "" || reverts != 0 {
		t.Fatal("failed cleanup lost ownership or reverted profiles prematurely")
	}
	if err := w.Cleanup(); !errors.Is(err, failure) {
		t.Fatalf("uninstall claimed success: %v", err)
	}
	assertOffMarker(t, w, true)
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	registry.fail = nil
	if err := w.Cleanup(); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, w, true)
	if len(*changes) != 4 || reverts != 1 {
		t.Fatalf("retry sequence %v reverts=%d", *changes, reverts)
	}
}

func TestWebRTCProfileFailuresPreserveOptOutAndUninstallState(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Set(false); err != nil {
		t.Fatal(err)
	}
	failure := errors.New("fixture: profile failure")
	w.ApplyProfiles = func() error { return failure }
	if err := w.Set(true); !errors.Is(err, failure) {
		t.Fatalf("lost apply callback failure: %v", err)
	}
	assertOffMarker(t, w, true)
	w.ApplyProfiles = nil
	w.RevertProfiles = func() error { return failure }
	if err := w.Cleanup(); !errors.Is(err, failure) {
		t.Fatalf("lost revert callback failure: %v", err)
	}
	assertOffMarker(t, w, true)
	if len(*changes) != 2 {
		t.Fatal("unexpected policy changes")
	}
	registry.readError = nil
	w.RevertProfiles = nil
	if err := w.Cleanup(); err != nil || len(*changes) != 2 {
		t.Fatalf("profile-only retry requested UAC: %v %v", err, *changes)
	}
	assertOffMarker(t, w, true)
}

func TestWebRTCVerifiesBothSetupAndCleanupPostconditions(t *testing.T) {
	for _, incomplete := range []string{"setup", "metadata", "policies"} {
		t.Run(incomplete, func(t *testing.T) {
			w, registry, _ := webRTCFixture(t)
			if incomplete != "setup" {
				if err := w.Ensure(); err != nil {
					t.Fatal(err)
				}
			}
			w.Change = func(_ string) error {
				if incomplete == "policies" {
					for _, policy := range webRTCPolicies {
						registry.put(WebRTCMetadataKey, policy.owner, Value{})
					}
				}
				return nil
			}
			var err error
			if incomplete == "setup" {
				err = w.Set(true)
			} else {
				err = w.Set(false)
				assertOffMarker(t, w, true)
			}
			if err == nil {
				t.Fatal("unverified change claimed success")
			}
		})
	}
}

func TestWebRTCStateErrorsDoNotPerformPartialCleanup(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := os.MkdirAll(filepath.Dir(w.StateFile), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(w.StateFile, []byte("invalid fixture state"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := w.Ensure(); err == nil || len(*changes) != 0 {
		t.Fatal("corrupt state silently enabled protection")
	}
	// Explicit commands recover corrupt state without interpreting its data.
	if err := w.Set(true); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, w, false)
	w.StateFile = ""
	if err := w.Set(false); err == nil || len(*changes) != 1 {
		t.Fatal("missing state path allowed unpersisted off")
	}
	w.StateFile = filepath.Join(t.TempDir(), "not-a-directory", "mode")
	if err := os.WriteFile(filepath.Dir(w.StateFile), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := w.Set(false); err == nil || len(*changes) != 1 {
		t.Fatal("failed state write performed cleanup")
	}
	registry.put(WebRTCMetadataKey, WebRTCChromeOwnerName, Value{"forged", "String"})
	if err := w.Cleanup(); err == nil || len(*changes) != 1 {
		t.Fatal("invalid ownership was accepted")
	}
}

func TestWebRTCSetupCancellationKeepsExplicitOptOutAndDoesNotApplyProfiles(t *testing.T) {
	w, _, _ := webRTCFixture(t)
	if err := w.Set(false); err != nil {
		t.Fatal(err)
	}
	w.Change = func(action string) error {
		if action != "on" {
			t.Fatalf("wrong action: %s", action)
		}
		return context.Canceled
	}
	w.ApplyProfiles = func() error { t.Fatal("cancelled setup applied profiles"); return nil }
	if err := w.Set(true); !errors.Is(err, context.Canceled) {
		t.Fatalf("lost setup cancellation: %v", err)
	}
	assertOffMarker(t, w, true)
}

func TestWebRTCMetadataWriteAndDeleteFailuresKeepRecoverableState(t *testing.T) {
	w, registry, _ := webRTCFixture(t)
	failure := errors.New("fixture: metadata write denied")
	registry.fail = func(key, _ string, _ Value) error {
		if key == WebRTCMetadataKey {
			return failure
		}
		return nil
	}
	if err := w.Ensure(); !errors.Is(err, failure) {
		t.Fatalf("lost intent-write error: %v", err)
	}
	if len(registry.writes) != 0 || registry.values[ChromePolicyKey][WebRtcPolicyName].Kind != "" {
		t.Fatal("policy was written without durable intent")
	}
	registry.fail = nil
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	registry.fail = func(key, _ string, value Value) error {
		if key == WebRTCMetadataKey && value.Kind == "" {
			return failure
		}
		return nil
	}
	if err := w.Set(false); !errors.Is(err, failure) {
		t.Fatalf("lost intent-delete error: %v", err)
	}
	assertOffMarker(t, w, true)
	if registry.values[ChromePolicyKey][WebRtcPolicyName].Kind != "" || registry.values[WebRTCMetadataKey][WebRTCChromeOwnerName].Kind == "" {
		t.Fatal("failed metadata deletion lost recovery information")
	}
	registry.fail = nil
	if err := w.Cleanup(); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, w, true)
}

func TestWebRTCReadBackErrorsPropagateWithoutClaimingSuccess(t *testing.T) {
	for _, action := range []string{"on", "off"} {
		t.Run(action, func(t *testing.T) {
			w, registry, _ := webRTCFixture(t)
			if action == "off" {
				if err := w.Ensure(); err != nil {
					t.Fatal(err)
				}
			}
			failure := errors.New("fixture: postcondition read denied")
			w.Change = func(action string) error {
				if err := registry.change(action); err != nil {
					return err
				}
				registry.readError = failure
				return nil
			}
			if err := w.Set(action == "on"); !errors.Is(err, failure) {
				t.Fatalf("lost postcondition-read error: %v", err)
			}
			if action == "off" {
				assertOffMarker(t, w, true)
			}
		})
	}
}

func TestWebRTCOnlyExplicitOnRemovesModeState(t *testing.T) {
	w, _, _ := webRTCFixture(t)
	if err := os.MkdirAll(w.StateFile, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w.StateFile, "keep"), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := w.Ensure(); err == nil {
		t.Fatal("unreadable state was ignored")
	}
	if err := w.Set(true); err == nil {
		t.Fatal("failed state removal was ignored")
	}
	if err := w.Set(false); err == nil {
		t.Fatal("failed atomic state replacement was ignored")
	}
	if err := w.Cleanup(); err != nil {
		t.Fatalf("browser cleanup tried to remove mode state: %v", err)
	}
	if _, err := os.Stat(filepath.Join(w.StateFile, "keep")); err != nil {
		t.Fatal("browser cleanup removed mode state")
	}
}

func TestWebRTCCleanupKeepsOptOutUntilExplicitOnOrFinalUninstall(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	if err := w.Ensure(); err != nil {
		t.Fatal(err)
	}
	if err := w.Set(false); err != nil {
		t.Fatal(err)
	}
	if err := w.Cleanup(); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, w, true)
	// A later uninstall step may fail. A new process must still respect the
	// user's choice rather than recreate the policies browser cleanup removed.
	registry.readError = errors.New("automatic setup must not read registry")
	restarted := &WebRTC{Registry: registry, StateFile: w.StateFile, Change: w.Change}
	if err := restarted.Ensure(); err != nil || len(*changes) != 2 {
		t.Fatalf("cleanup lost opt-out: %v changes=%v", err, *changes)
	}
	registry.readError = nil
	if err := restarted.Set(true); err != nil {
		t.Fatal(err)
	}
	assertOffMarker(t, restarted, false)
}

func TestWebRTCOffErrorClaimsOptOutOnlyAfterPersistenceSucceeds(t *testing.T) {
	for _, persisted := range []bool{false, true} {
		t.Run(map[bool]string{false: "state write fails", true: "cleanup fails"}[persisted], func(t *testing.T) {
			w, _, _ := webRTCFixture(t)
			failure := errors.New("fixture: profile cleanup failed")
			callbacks := 0
			w.RevertProfiles = func() error { callbacks++; return failure }
			if !persisted {
				w.StateFile = ""
			}
			err := w.Set(false)
			if err == nil {
				t.Fatal("failed off operation reported success")
			}
			claim := strings.Contains(err.Error(), "automatic WebRTC setup is now off")
			if claim != persisted {
				t.Fatalf("incorrect opt-out claim: %v", err)
			}
			if persisted {
				assertOffMarker(t, w, true)
				if !errors.Is(err, failure) || !strings.Contains(err.Error(), "retry 'sbc webrtc off'") || callbacks != 1 {
					t.Fatalf("cleanup error lost cause or retry instructions: %v", err)
				}
			} else if callbacks != 0 {
				t.Fatal("cleanup ran after a failed state write")
			}
		})
	}
}

func TestWebRTCPreconfiguredPoliciesAndUnownedOffNeedNoApproval(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	for _, policy := range webRTCPolicies {
		registry.put(policy.key, policy.name, policy.want)
	}
	for range 2 {
		if err := w.Ensure(); err != nil {
			t.Fatal(err)
		}
		if err := w.Set(false); err != nil {
			t.Fatal(err)
		}
		if err := w.Set(true); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Cleanup(); err != nil || len(*changes) != 0 {
		t.Fatalf("unowned policies requested UAC: %v %v", err, *changes)
	}
	remaining, err := w.Remaining()
	if err != nil || len(remaining) != len(webRTCPolicies) {
		t.Fatalf("pre-existing policies removed: %v %v", remaining, err)
	}
}

func awaitWebRTCOperation(t *testing.T, done <-chan error) {
	t.Helper()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("WebRTC operation did not finish")
	}
}

func TestWebRTCCleanupWaitsForCompleteSetup(t *testing.T) {
	for _, setup := range []string{"automatic", "explicit"} {
		for _, stage := range []string{"policies", "profiles"} {
			for _, cleanup := range []string{"off", "uninstall"} {
				t.Run(setup+"/"+stage+"/"+cleanup, func(t *testing.T) {
					w, registry, _ := webRTCFixture(t)
					entered, resume := make(chan struct{}), make(chan struct{})
					unblock := sync.OnceFunc(func() { close(resume) })
					defer unblock()
					pause := func() {
						close(entered)
						<-resume
					}
					profile := filepath.Join(filepath.Dir(w.StateFile), "profile.js")
					w.Change = func(action string) error {
						if action == "on" && stage == "policies" {
							pause()
						}
						return registry.change(action)
					}
					w.ApplyProfiles = func() error {
						if stage == "profiles" {
							pause()
						}
						return os.WriteFile(profile, []byte("fixture protection"), 0600)
					}
					w.RevertProfiles = func() error {
						err := os.Remove(profile)
						if os.IsNotExist(err) {
							return nil
						}
						return err
					}
					if err := os.MkdirAll(filepath.Dir(w.StateFile), 0700); err != nil {
						t.Fatal(err)
					}
					other := *w // Separate managers must share the operation lock.
					setupDone := make(chan error, 1)
					go func() {
						if setup == "automatic" {
							setupDone <- w.Ensure()
						} else {
							setupDone <- w.Set(true)
						}
					}()
					select {
					case <-entered:
					case <-time.After(5 * time.Second):
						t.Fatal("setup did not reach the paused stage")
					}
					cleanupDone := make(chan error, 1)
					go func() {
						if cleanup == "off" {
							cleanupDone <- other.Set(false)
						} else {
							cleanupDone <- other.Cleanup()
						}
					}()
					select {
					case err := <-cleanupDone:
						t.Errorf("cleanup returned before setup finished: %v", err)
						cleanupDone <- err
					case <-time.After(50 * time.Millisecond):
					}
					unblock()
					awaitWebRTCOperation(t, setupDone)
					awaitWebRTCOperation(t, cleanupDone)
					if cleanup == "off" {
						assertOffMarker(t, w, true)
						if err := other.Ensure(); err != nil {
							t.Fatal(err)
						}
					}
					for _, policy := range webRTCPolicies {
						if registry.values[policy.key][policy.name].Kind != "" || registry.values[WebRTCMetadataKey][policy.owner].Kind != "" {
							t.Error("setup recreated policies after cleanup")
						}
					}
					if _, err := os.Stat(profile); !os.IsNotExist(err) {
						t.Errorf("setup recreated the profile after cleanup: %v", err)
					}
				})
			}
		}
	}
}

func TestWebRTCQueuedSetupRespectsCompletedOptOut(t *testing.T) {
	w, registry, changes := webRTCFixture(t)
	entered, resume := make(chan struct{}), make(chan struct{})
	unblock := sync.OnceFunc(func() { close(resume) })
	defer unblock()
	w.RevertProfiles = func() error { close(entered); <-resume; return nil }
	other := *w
	offDone := make(chan error, 1)
	go func() { offDone <- w.Set(false) }()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("off did not reach profile cleanup")
	}
	registry.readError = errors.New("opt-out must prevent registry access")
	setupDone := make(chan error, 1)
	go func() { setupDone <- other.Ensure() }()
	select {
	case err := <-setupDone:
		t.Errorf("setup returned before cleanup finished: %v", err)
		setupDone <- err
	case <-time.After(50 * time.Millisecond):
	}
	unblock()
	awaitWebRTCOperation(t, offDone)
	awaitWebRTCOperation(t, setupDone)
	assertOffMarker(t, w, true)
	if len(*changes) != 0 {
		t.Fatalf("automatic setup reversed opt-out: %v", *changes)
	}
}
