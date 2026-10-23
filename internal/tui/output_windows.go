//go:build windows

package tui

import (
	"io"
	"os"

	"golang.org/x/sys/windows"
)

func prepareOutput(w io.Writer) (func() error, error) {
	file, ok := w.(*os.File)
	if !ok || !IsTerminal(file) {
		return nil, nil
	}
	handle := windows.Handle(file.Fd())
	var mode uint32
	if err := windows.GetConsoleMode(handle, &mode); err != nil {
		return nil, err
	}
	if err := windows.SetConsoleMode(handle, mode|windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING); err != nil {
		return nil, err
	}
	return func() error { return windows.SetConsoleMode(handle, mode) }, nil
}
