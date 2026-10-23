//go:build !windows

package tui

import "io"

func prepareOutput(io.Writer) (func() error, error) { return nil, nil }
