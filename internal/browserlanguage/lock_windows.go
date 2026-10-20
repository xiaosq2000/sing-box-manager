package browserlanguage

import (
	"os"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

func lock(string) (func(), error) {
	return nil, i18n.New("Chrome language matching is available on Linux only")
}

func openRead(path string) (*os.File, error) { return os.Open(path) }

func ownedFile(string) error {
	return i18n.New("Chrome language matching is available on Linux only")
}
