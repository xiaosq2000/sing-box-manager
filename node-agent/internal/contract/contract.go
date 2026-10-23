// Package contract contains the G0 fixture contract, not the production node API.
package contract

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

func UUID() string {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		panic(err)
	}
	id[6] = (id[6] & 15) | 64
	id[8] = (id[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:])
}

func Hash(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

type Identity struct {
	NodeID   string `json:"node_id"`
	SourceID string `json:"source_id"`
	Secret   string `json:"secret"`
	Origin   string `json:"origin"`
	CAFile   string `json:"ca_file"`
}

type Entry struct {
	CredentialID string `json:"credential_id"`
	WireName     string `json:"wire_name"`
}

type Bundle struct {
	Schema       int             `json:"schema"`
	NodeID       string          `json:"node_id"`
	Generation   int64           `json:"generation"`
	Config       json.RawMessage `json:"config"`
	ConfigSHA256 string          `json:"config_sha256"`
	StatsAddress string          `json:"stats_address"`
	Roster       []Entry         `json:"roster"`
}

func (b Bundle) Validate(nodeID string) error {
	if b.Schema != 1 || b.NodeID != nodeID || b.Generation <= 0 || len(b.Roster) != 1 || !json.Valid(b.Config) || Hash(b.Config) != b.ConfigSHA256 {
		return errors.New("invalid G0 bundle")
	}
	return nil
}

type Snapshot struct {
	SourceID     string `json:"source_id"`
	CredentialID string `json:"credential_id"`
	UsageDate    string `json:"usage_date"`
	Version      int64  `json:"version"`
	Upload       int64  `json:"upload"`
	Download     int64  `json:"download"`
	Digest       string `json:"digest"`
}

// Digest uses sorted JSON keys, matching Python's compact sort_keys encoding.
func (s Snapshot) ComputeDigest() string {
	data, _ := json.Marshal(map[string]any{
		"source_id": s.SourceID, "credential_id": s.CredentialID,
		"usage_date": s.UsageDate, "version": s.Version,
		"upload": s.Upload, "download": s.Download,
	})
	return Hash(data)
}

func (s Snapshot) Validate() error {
	if _, err := time.Parse("2006-01-02", s.UsageDate); err != nil {
		return errors.New("invalid usage date")
	}
	if s.Version < 1 || s.Upload < 0 || s.Download < 0 || s.Digest != s.ComputeDigest() {
		return errors.New("invalid snapshot")
	}
	return nil
}
