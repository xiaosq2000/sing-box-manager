// Command sbc is the sing-box-manager client. Shells find it on PATH after the
// rc file runs `sbc init`.
package main

import (
	"fmt"
	"os"

	"github.com/xiaosq2000/sing-box-manager/internal/cli"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	if err := cli.ConfigureLanguage(); err != nil && (len(os.Args) < 2 || os.Args[1] != "lang") {
		// 'sbc lang' reports the same problem itself and can repair it.
		fmt.Fprintf(os.Stderr, "sbc: %v\n", err)
	}
	os.Exit(cli.Run(cli.DefaultEnv(version, os.Stdin, os.Stdout, os.Stderr), os.Args[1:]))
}
