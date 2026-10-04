// Command sbc is the sing-box-manager client. Shells find it on PATH after the
// rc file runs `sbc init`.
package main

import (
	"os"

	"github.com/xiaosq2000/sing-box-manager/internal/cli"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	os.Exit(cli.Run(cli.DefaultEnv(version, os.Stdin, os.Stdout, os.Stderr), os.Args[1:]))
}
