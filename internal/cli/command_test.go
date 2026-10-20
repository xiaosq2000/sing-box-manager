package cli

import (
	"bytes"
	"io"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

type forbiddenInput struct{ t *testing.T }

func (input forbiddenInput) Read([]byte) (int, error) {
	input.t.Fatal("help or validation read standard input")
	return 0, io.EOF
}

func inertEnv(t *testing.T) (Env, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	// Leave operational callbacks unset. A regression must fail the test,
	// never fall through to this machine's services or browser settings.
	env := Env{Version: "1.2.3", OS: runtime.GOOS, Stdin: forbiddenInput{t}, Stdout: out, Stderr: errOut}
	env.Layout = func() (paths.Layout, error) {
		t.Fatal("help or validation inspected the installation")
		return paths.Layout{}, nil
	}
	return env, out, errOut
}

func TestCommandHelpNeverInvokesActions(t *testing.T) {
	env, out, errOut := inertEnv(t)
	exit := 0
	root := newCommand(env, &exit)
	root.InitDefaultHelpCmd()
	for _, command := range root.Commands() {
		if command.Hidden {
			continue
		}
		for _, args := range [][]string{{command.Name(), "--help"}, {command.Name(), "-h"}, {"help", command.Name()}} {
			out.Reset()
			errOut.Reset()
			if code := Run(env, args); code != 0 || !strings.Contains(out.String(), "Usage: sbc "+command.Name()) || errOut.Len() != 0 {
				t.Fatalf("%v: code %d, stdout %q, stderr %q", args, code, out, errOut)
			}
		}
	}
	for _, args := range [][]string{{"privacy", "browser", "--help"}, {"privacy", "browser", "webrtc", "--help"}, {"privacy", "browser", "language", "--help"}, {"refresh", "auto", "--help"}, {"upgrade", "interval", "--help"}, {"uninstall", "--yes", "--help"}, {"install", "--auth", "on", "--help"}, {"docker", "on", "--yes", "--help"}} {
		if code := Run(env, args); code != 0 {
			t.Fatalf("%v: code %d", args, code)
		}
	}
}

func TestCLIValidationRejectsInputBeforeActions(t *testing.T) {
	env, out, errOut := inertEnv(t)
	cases := [][]string{
		{"status", "extra"}, {"route", "china", "extra"}, {"protocol", "naive", "extra"},
		{"port", "1080", "extra"}, {"on", "extra"}, {"off", "--bogus"},
		{"start", "--bogus"}, {"stop", "extra"}, {"restart", "extra"}, {"run", "extra"},
		{"ip", "extra"}, {"upgrade", "extra"}, {"refresh", "extra"},
		{"refresh", "auto", "maybe"}, {"upgrade", "auto", "on", "extra"}, {"refresh", "interval", "0s"}, {"upgrade", "interval", "1h", "extra"}, {"version", "extra"},
		{"env", "maybe"}, {"lang", "fr"}, {"lang", "en", "extra"}, {"desktop", "on", "extra"}, {"privacy", "browser", "maybe"}, {"privacy", "browser", "on", "extra"},
		{"privacy", "browser", "language", "on", "extra"}, {"privacy", "browser", "language", "maybe"},
		{"privacy", "browser", "webrtc", "on", "extra"}, {"privacy", "browser", "webrtc", "--bogus"},
		{"docker", "on", "off"}, {"docker", "--bogus"}, {"uninstall", "--yes", "extra"},
		{"link", "set", "extra"}, {"link", "show"}, {"init", "fish"},
		{"completion"}, {"completion", "fish"}, {"help", "bogus"}, {"help", "status", "extra"},
		{"install", "--auth", "maybe"}, {"install", "--shell", "fish"}, {"install", "--shell"},
		{"install", "--no-rc=maybe"}, {"install", "--protocol", "naive", "extra"},
		{"install", "https://vpn.example.com/sub/fixture-secret-token"},
		{"speed", "--download=0"}, {"speed", "--download", "100"}, {"speed", "--download", "nonsense"},
		{"speed", "50", "--download"}, {"speed", "--all", "--all"}, {"speed", "--download", "--download"},
	}
	for _, args := range cases {
		out.Reset()
		errOut.Reset()
		if code := Run(env, args); code != 2 || out.Len() != 0 || errOut.Len() == 0 {
			t.Errorf("%v: code %d, stdout %q, stderr %q", args, code, out, errOut)
		}
		if strings.Contains(errOut.String(), "fixture-secret-token") {
			t.Fatal("argument rejection disclosed the subscription credential")
		}
	}
}

func TestCommandHelpAndParserErrorsAreChinese(t *testing.T) {
	i18n.SetChinese(true)
	defer i18n.SetChinese(false)
	env, out, errOut := inertEnv(t)
	if code := Run(env, []string{"install", "--help"}); code != 0 || !strings.Contains(out.String(), "用法：") || !strings.Contains(out.String(), "初始协议名称") || strings.Contains(out.String(), "Flags:") {
		t.Fatalf("Chinese help: %d, %s", code, out)
	}
	for _, entry := range []struct {
		args []string
		want string
	}{
		{[]string{"status", "--bogus"}, "未知选项"},
		{[]string{"install", "--shell"}, "需要一个值"},
		{[]string{"install", "--no-rc=maybe"}, "不适用于选项"},
		{[]string{"route", "china", "extra"}, "实际收到"},
		{[]string{"env", "maybe"}, "只接受"},
		{[]string{"bogus"}, "未知命令"},
		{[]string{"speed", "--all", "--all"}, "不能重复"},
	} {
		errOut.Reset()
		if code := Run(env, entry.args); code != 2 || !strings.Contains(errOut.String(), entry.want) || !strings.Contains(errOut.String(), "查看用法") {
			t.Errorf("%v: code %d, stderr %q", entry.args, code, errOut)
		}
	}
}

func TestCommandLegacyFlagSyntaxAndSpeedSizes(t *testing.T) {
	h := newHarness(t)
	exit := 0
	root := newCommand(h.env, &exit)
	cmd, _, _ := root.Find([]string{"install"})
	args := normalizeArgs(root, []string{"install", "-shell=zsh", "-no-rc", "-auth", "on"})
	if err := cmd.ParseFlags(args[1:]); err != nil {
		t.Fatal(err)
	}
	if err := cmd.ValidateArgs(cmd.Flags().Args()); err != nil {
		t.Fatal(err)
	}
	valueArgs := normalizeArgs(root, []string{"install", "--protocol", "-shell"})
	if !slices.Equal(valueArgs, []string{"install", "--protocol", "-shell"}) {
		t.Fatalf("rewrote a dash-prefixed flag value: %v", valueArgs)
	}
	for _, size := range []string{"5", "99"} {
		h.useDownloads(t, map[string]time.Duration{"trojan": time.Second, "naive": time.Second}, nil)
		for _, args := range [][]string{{"speed", "--download", size}, {"speed", "--all", "--download", size}, {"speed", "--download=" + size, "--all"}} {
			h.out.Reset()
			h.errOut.Reset()
			if code := Run(h.env, args); code != 0 || !strings.Contains(h.out.String(), "Downloading "+size+" MB") {
				t.Fatalf("%v: code %d, stdout %s, stderr %s", args, code, h.out, h.errOut)
			}
		}
	}
}
