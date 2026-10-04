package trust

import (
	"encoding/base64"
	"errors"
	"strings"
	"testing"
)

// Signed by sing_box_manager.release.client_downloads with the seed bytes 0..31,
// so a change on either side of the format fails here.
const (
	pythonKey       = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
	pythonSignature = "Kkh7uOWOR44x8O9fUGToRa90OxJH6V94OA7d79lht9hxKtOi9tfyfwhIUL5BgriGEzmZQcs41YL/+JqPnkyiDQ==\n"
	pythonManifest  = "ewogICJmaWxlcyI6IFsKICAgIHsKICAgICAgInBhdGgiOiAicnVsZXMvYS5zcnMiLAogICAgICAic2hhMjU2IjogIjAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAiLAogICAgICAic2l6ZSI6IDUKICAgIH0KICBdLAogICJzYmMiOiAiYWJjIiwKICAic2luZ19ib3giOiAiMS4xNC4yIiwKICAidmVyc2lvbiI6IDEKfQo="
)

func payload(t *testing.T) []byte {
	t.Helper()
	data, err := base64.StdEncoding.DecodeString(pythonManifest)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestVerifyAcceptsWhatTheReleaseSigned(t *testing.T) {
	manifest, err := Verify(payload(t), []byte(pythonSignature), []string{"eaDFZO3F8e1HG1pfCT03lKZts2R1fe9817b6qsyjCmw=", pythonKey})
	if err != nil {
		t.Fatal(err)
	}
	if manifest.SBC != "abc" || manifest.SingBox != "1.14.2" {
		t.Errorf("got %+v", manifest)
	}
	file, ok := manifest.Lookup("rules/a.srs")
	if !ok || file.Size != 5 {
		t.Fatalf("got %+v", file)
	}
	if err := file.Check([]byte("12345")); err == nil {
		t.Error("data with a different digest passed")
	}
}

func TestVerifyRejectsOtherKeysAndChangedBytes(t *testing.T) {
	data := payload(t)
	if _, err := Verify(data, []byte(pythonSignature), Keys); !errors.Is(err, ErrUntrusted) {
		t.Errorf("another key: got %v", err)
	}
	changed := append([]byte{}, data...)
	changed[len(changed)-2] = ' '
	if _, err := Verify(changed, []byte(pythonSignature), []string{pythonKey}); !errors.Is(err, ErrUntrusted) {
		t.Errorf("changed bytes: got %v", err)
	}
	if _, err := Verify(data, []byte("not base64"), []string{pythonKey}); !errors.Is(err, ErrUntrusted) {
		t.Errorf("bad signature: got %v", err)
	}
	if _, err := Verify(data, []byte(pythonSignature), []string{"short"}); err == nil || !strings.Contains(err.Error(), "malformed trusted key") {
		t.Errorf("bad key: got %v", err)
	}
}

func TestTheBuiltInKeysAreWellFormed(t *testing.T) {
	for _, key := range Keys {
		raw, err := base64.StdEncoding.DecodeString(key)
		if err != nil || len(raw) != 32 {
			t.Errorf("%q is not a 32-byte base64 key", key)
		}
	}
}
