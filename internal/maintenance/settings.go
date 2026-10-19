// Package maintenance stores the two independent client maintenance policies.
package maintenance

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

const DefaultInterval = 6 * time.Hour

// Policy controls one background action. Manual actions ignore Enabled.
type Policy struct {
	Enabled     bool      `json:"enabled"`
	Seconds     int64     `json:"interval_seconds"`
	LastAttempt time.Time `json:"last_attempt,omitempty"`
	LastSuccess time.Time `json:"last_success,omitempty"`
	Result      string    `json:"result,omitempty"`
}

type Settings struct {
	Version   int    `json:"version"`
	Refresh   Policy `json:"refresh"`
	Upgrade   Policy `json:"upgrade"`
	Scheduled bool   `json:"scheduled"`
}

func Defaults() Settings {
	policy := Policy{Enabled: true, Seconds: int64(DefaultInterval / time.Second)}
	return Settings{Version: 1, Refresh: policy, Upgrade: policy}
}

func File(l paths.Layout) string { return filepath.Join(l.Config, "maintenance.json") }

// Load does not write defaults. Missing settings mean both policies are enabled.
// An invalid existing file is an error, never permission to enable a policy.
func Load(l paths.Layout) (Settings, error) {
	data, err := os.ReadFile(File(l))
	if errors.Is(err, os.ErrNotExist) {
		return Defaults(), nil
	}
	if err != nil {
		return Settings{}, err
	}
	var s Settings
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&s); err != nil {
		return s, i18n.Errorf("invalid maintenance settings: %w", err)
	}
	if decoder.Decode(new(any)) != io.EOF {
		return s, i18n.New("invalid maintenance settings: trailing data")
	}
	return s, s.Validate()
}

func (s Settings) Validate() error {
	if s.Version != 1 {
		return i18n.New("unsupported maintenance settings version")
	}
	for _, p := range []Policy{s.Refresh, s.Upgrade} {
		if p.Seconds < 60 || p.Seconds > 30*24*60*60 {
			return i18n.New("maintenance interval must be between 1m and 720h")
		}
		if p.Result != "" && p.Result != "ok" && p.Result != "failed" && p.Result != "running" {
			return i18n.New("invalid maintenance result")
		}
	}
	return nil
}

func (s Settings) Save(l paths.Layout) error {
	if err := s.Validate(); err != nil {
		return err
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return paths.WriteFile(File(l), append(data, '\n'), 0o600)
}

func (s *Settings) Policy(action string) *Policy {
	if action == "refresh" {
		return &s.Refresh
	}
	return &s.Upgrade
}

func Interval(value string) (int64, error) {
	d, err := time.ParseDuration(value)
	if err != nil || d < time.Minute || d > 30*24*time.Hour || d%time.Second != 0 {
		return 0, i18n.New("interval must be whole seconds between 1m and 720h, for example 30m, 6h, or 24h")
	}
	return int64(d / time.Second), nil
}

func (p Policy) Interval() string {
	value := (time.Duration(p.Seconds) * time.Second).String()
	if strings.HasSuffix(value, "m0s") {
		value = strings.TrimSuffix(value, "0s")
	}
	if strings.HasSuffix(value, "h0m") {
		value = strings.TrimSuffix(value, "0m")
	}
	return value
}

// Delay spreads clients without making short schedules wait half an hour.
func (p Policy) Delay() int64 { return min(p.Seconds/12, 30*60) }
