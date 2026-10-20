package browserlanguage

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

// Keep unrelated JSON as RawMessage, including numbers above 2^53.
type preferences struct {
	root     map[string]json.RawMessage
	intl     map[string]json.RawMessage
	hadIntl  bool
	original []byte
	mode     os.FileMode
}

func readPreferences(path string) (preferences, error) {
	prefs := preferences{intl: map[string]json.RawMessage{}}
	data, mode, err := readFile(path, 32<<20)
	if err != nil {
		return prefs, err
	}
	if err := ownedFile(path); err != nil {
		return prefs, err
	}
	prefs.original, prefs.mode = data, mode
	prefs.root, err = object(data)
	if err == nil {
		if value, ok := prefs.root["intl"]; ok {
			prefs.hadIntl = true
			prefs.intl, err = object(value)
		}
	}
	if err != nil {
		return prefs, i18n.Errorf("Chrome preferences are invalid at %s. sbc changed nothing", path)
	}
	return prefs, nil
}

// object rejects duplicate keys and trailing values in the objects we edit.
func object(data []byte) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, fmt.Errorf("expected a JSON object")
	}
	result := map[string]json.RawMessage{}
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		key, ok := token.(string)
		if !ok {
			return nil, fmt.Errorf("expected a JSON key")
		}
		if _, exists := result[key]; exists {
			return nil, fmt.Errorf("duplicate JSON key")
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, err
		}
		result[key] = value
	}
	if _, err := decoder.Token(); err != nil {
		return nil, err
	}
	if decoder.Decode(new(any)) != io.EOF {
		return nil, fmt.Errorf("unexpected data after JSON object")
	}
	return result, nil
}

func (m *Manager) writePreferences(root, path string, prefs preferences, removeIntl bool) error {
	if removeIntl {
		delete(prefs.root, "intl")
	} else {
		value, err := json.Marshal(prefs.intl)
		if err != nil {
			return err
		}
		prefs.root["intl"] = value
	}
	data, err := json.Marshal(prefs.root)
	if err != nil {
		return err
	}
	// Recheck both Chrome's singleton and the complete file immediately before
	// replacement. The user must not start Chrome concurrently with this command.
	if err := closed(root); err != nil {
		return err
	}
	current, _, err := readFile(path, 32<<20)
	if err != nil {
		return err
	}
	if !bytes.Equal(current, prefs.original) {
		return i18n.New("Chrome preferences changed during the operation. Exit Chrome and retry")
	}
	if err := ownedFile(path); err != nil {
		return err
	}
	return m.write(path, append(data, '\n'), prefs.mode)
}

func closed(root string) error {
	for _, name := range []string{"SingletonLock", "SingletonSocket", "SingletonCookie"} {
		if _, err := os.Lstat(filepath.Join(root, name)); err == nil {
			return i18n.New("Chrome is running or a singleton file remains. Exit Chrome completely. Do not start Chrome during the command")
		} else if !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}

// safePath refuses symlinks in each existing component, including dangling ones.
func safePath(path string) error {
	if !filepath.IsAbs(path) {
		return i18n.New("Chrome language paths must be absolute")
	}
	current := string(filepath.Separator)
	for _, part := range strings.Split(filepath.Clean(path), string(filepath.Separator)) {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return i18n.Errorf("Chrome language path is a symbolic link: %s", current)
		}
	}
	return nil
}

func readFile(path string, limit int64) ([]byte, os.FileMode, error) {
	if err := safePath(path); err != nil {
		return nil, 0, err
	}
	file, err := openRead(path)
	if err != nil {
		return nil, 0, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, 0, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, 0, i18n.Errorf("Chrome language file is not a bounded regular file: %s", path)
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if int64(len(data)) > limit {
		return nil, 0, i18n.Errorf("Chrome language file is not a bounded regular file: %s", path)
	}
	return data, info.Mode().Perm(), err
}

func (m *Manager) write(path string, data []byte, mode os.FileMode) error {
	if m.Write != nil {
		return m.Write(path, data, mode)
	}
	return atomicWrite(path, data, mode)
}

func atomicWrite(path string, data []byte, mode os.FileMode) error {
	if err := safePath(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".sbc-language-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err = file.Chmod(mode); err == nil {
		_, err = file.Write(data)
	}
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err := safePath(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
