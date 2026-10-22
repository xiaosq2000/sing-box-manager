package diagnostic

import (
	"errors"
	"io"
	"os"
)

// ReadRegular refuses links and devices before opening, then checks identity
// again. Platform open helpers prevent a FIFO replacement from blocking open.
func ReadRegular(path string, limit int64) ([]byte, error) {
	before, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() || before.Size() > limit {
		return nil, errors.New("invalid regular file")
	}
	file, err := openRegular(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	after, err := file.Stat()
	if err != nil || !after.Mode().IsRegular() || after.Size() > limit || !os.SameFile(before, after) {
		return nil, errors.New("changed regular file")
	}
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, errors.New("regular file exceeds limit")
	}
	return data, nil
}
