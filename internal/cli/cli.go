// Package cli parses the sbc command line and runs its commands.
package cli

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/desktop"
	"github.com/xiaosq2000/sing-box-manager/internal/docker"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/install"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
	"github.com/xiaosq2000/sing-box-manager/internal/probe"
	"github.com/xiaosq2000/sing-box-manager/internal/service"
	"github.com/xiaosq2000/sing-box-manager/internal/shell"
	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
	"github.com/xiaosq2000/sing-box-manager/internal/trust"
	"github.com/xiaosq2000/sing-box-manager/internal/winsettings"
)

const usage = `Usage: sbc <command>

Setup:
  install [--shell bash|zsh] [--no-rc] [--auth on|off] [--route NAME] [--protocol NAME]
                     Set up from a subscription link, read from standard input
  init bash|zsh      Print the shell code the rc file runs
  link [set]         Show the subscription link, or replace it from standard input
  upgrade            Install a newer sbc and sing-box from the signed release
  uninstall [--yes]  Stop the service and remove sbc's files

Shells and programs:
  on, off            Point every shell at the proxy, or stop, from its next prompt,
                     and the desktop too after 'desktop on'; Windows applies to new programs
  env [on|off]       Print shell variables; PowerShell: sbc env | Invoke-Expression
  desktop [on|off]   Show or switch the desktop proxy (GNOME, macOS, Windows)
  docker [on|off] [--yes]
                     Show or switch the Docker daemon's proxy (Linux); restarts Docker

Proxy:
  status             Show the service, route, protocol, port, shells and traffic
  ip                 Show where Gemini, sites abroad and sites in China place you
  speed [--all] [--download [MB]]
                     Time a small page through the protocol in use, or download
                     20 MB or the given size through it; --all tests every protocol
  route [name]       Show or switch the route
  protocol [name]    Show or switch the protocol
  port [number]      Show or change the local proxy port
  update             Refresh the config from the subscription now
  start, stop, restart
                     Control the service that runs sing-box
  run                Run sing-box in the foreground, where no service manager runs it

  help, version
`

const usageZH = `用法：sbc <命令>

安装：
  install [--shell bash|zsh] [--no-rc] [--auth on|off] [--route NAME] [--protocol NAME]
                     用订阅链接安装，链接从标准输入读取
  init bash|zsh      输出 rc 文件要运行的 shell 代码
  link [set]         显示订阅链接，或从标准输入读取新链接来替换它
  upgrade            从签名的发布中安装更新的 sbc 和 sing-box
  uninstall [--yes]  停止服务并删除 sbc 的文件

shell 与程序：
  on, off            让每个 shell 从下一个提示符起使用或停用代理，
                     运行过 'desktop on' 后桌面也一起切换；Windows 对新程序生效
  env [on|off]       输出 shell 代理变量；PowerShell：sbc env | Invoke-Expression
  desktop [on|off]   显示或切换桌面代理（GNOME、macOS、Windows）
  docker [on|off] [--yes]
                     显示或切换 Docker 守护进程的代理（Linux），会重启 Docker

代理：
  status             显示服务、路由、协议、端口、shell 的状态和流量
  ip                 显示 Gemini、境外网站和境内网站看到的你的位置
  speed [--all] [--download [MB]]
                     测试经当前协议打开一个小网页的用时，或经它下载 20 MB 或指定
                     大小；加 --all 则测试每种协议
  route [name]       显示或切换路由
  protocol [name]    显示或切换协议
  port [number]      显示或更改本地代理端口
  update             立即从订阅更新配置
  start, stop, restart
                     控制运行 sing-box 的服务
  run                在没有服务管理器的环境中，于前台运行 sing-box

  help, version
`

// Env is what commands read and write, so tests can replace it.
type Env struct {
	// Windows holds the per-user Windows environment settings.
	Windows *winsettings.Environment
	Version string
	// OS is the platform sbc runs on, as runtime.GOOS names it.
	OS     string
	Stdin  io.Reader
	Stdout io.Writer
	Stderr io.Writer
	Layout func() (paths.Layout, error)
	// Exec replaces the process with another program. On Windows it runs the
	// program as a child instead, writing its output to the writer and its
	// process id to pidFile.
	Exec func(binary string, args []string, output io.Writer, pidFile string) error
	// Installer returns the installer for a layout.
	Installer func(layout paths.Layout, log io.Writer) *install.Installer
	// Service returns the service manager for the sbc at path.
	Service func(sbc string) (service.Manager, error)
	// Self returns the path of the running sbc.
	Self func() (string, error)
	// Home returns the home directory, whose rc files sbc edits.
	Home func() (string, error)
	// Desktop returns the proxy settings of the desktop this session runs in.
	Desktop func() (desktop.Desktop, error)
	// Docker returns the Docker daemon's proxy drop-in.
	Docker func() (*docker.Daemon, error)
	// Sites are the pages 'sbc ip' asks and 'sbc speed --download' fetches.
	Sites probe.Sites
	// TimeDownload fetches a page through a client and reports how many bytes
	// arrived in how long.
	TimeDownload func(client *http.Client, page string, limit time.Duration) (int64, time.Duration, error)
}

// DefaultEnv runs commands against this machine.
func DefaultEnv(version string, stdin io.Reader, stdout, stderr io.Writer) Env {
	return Env{
		Windows: &winsettings.Environment{Registry: winsettings.PowerShell{}},
		Version: version,
		OS:      runtime.GOOS,
		Stdin:   stdin,
		Stdout:  stdout,
		Stderr:  stderr,
		Layout:  paths.Default,
		Exec:    execProcess,
		Installer: func(layout paths.Layout, log io.Writer) *install.Installer {
			return &install.Installer{
				Client: install.NewClient(),
				Keys:   trust.Keys,
				Layout: layout,
				OS:     runtime.GOOS,
				Arch:   runtime.GOARCH,
				Log:    log,
			}
		},
		Service: func(sbc string) (service.Manager, error) {
			return service.New(runtime.GOOS, sbc, service.Exec)
		},
		Self: os.Executable,
		Home: os.UserHomeDir,
		Desktop: func() (desktop.Desktop, error) {
			return desktop.New(runtime.GOOS, os.Getenv, desktop.Exec)
		},
		Docker: func() (*docker.Daemon, error) {
			return docker.New(runtime.GOOS, docker.Exec)
		},
		Sites:        probe.DefaultSites,
		TimeDownload: probe.Download,
	}
}

// Run runs the command that args name and returns the process exit code.
func Run(env Env, args []string) int {
	if len(args) == 0 {
		fmt.Fprint(env.Stdout, i18n.Pick(usage, usageZH))
		return 0
	}
	switch args[0] {
	case "help", "-h", "--help":
		fmt.Fprint(env.Stdout, i18n.Pick(usage, usageZH))
		return 0
	case "version", "--version":
		fmt.Fprintf(env.Stdout, "sbc %s\n", env.Version)
		return 0
	case "install":
		return runInstall(env, args[1:])
	case "env":
		return runEnv(env, args[1:])
	case "on":
		return runOn(env)
	case "off":
		return runOff(env)
	case "port":
		return runPort(env, args[1:])
	case "link":
		return runLink(env, args[1:])
	case "init":
		return runInit(env, args[1:])
	case "start", "stop", "restart":
		return runService(env, args[0])
	case "upgrade":
		return runUpgrade(env)
	case "run":
		return runSingBox(env)
	case "status":
		return runStatus(env)
	case "ip":
		return runIP(env)
	case "speed":
		return runSpeed(env, args[1:])
	case "route":
		return runSwitch(env, args[1:], "route")
	case "protocol":
		return runSwitch(env, args[1:], "protocol")
	case "update":
		return runUpdate(env)
	case "uninstall":
		return runUninstall(env, args[1:])
	case "desktop":
		return runDesktop(env, args[1:])
	case "docker":
		return runDocker(env, args[1:])
	default:
		fmt.Fprintf(env.Stderr, i18n.T("sbc: unknown command %q\nRun 'sbc help' for usage.\n"), args[0])
		return 2
	}
}

func fail(env Env, err error) int {
	fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
	return 1
}

// prompt asks for an answer when standard input is a terminal, and stays quiet
// when a script pipes the answer in.
func prompt(env Env, text string) {
	if isTerminal(env.Stdin) {
		fmt.Fprint(env.Stderr, i18n.T(text))
	}
}

func runInstall(env Env, args []string) int {
	options, err := parseInstallFlags(args)
	if err != nil {
		fmt.Fprintf(env.Stderr, "sbc: %v\n", err)
		return 2
	}
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	// The link is a credential, so it comes from standard input rather than an
	// argument, which would reach shell history and the process list.
	prompt(env, "Subscription link: ")
	line, err := bufio.NewReader(env.Stdin).ReadString('\n')
	if err != nil && line == "" {
		return fail(env, i18n.Errorf("no subscription link was given"))
	}
	link, err := install.ParseLink(line)
	if err != nil {
		return fail(env, err)
	}
	var previousEnvironment map[string]winsettings.Value
	if env.OS == "windows" {
		if previous, err := readLocal(layout); err == nil {
			previousEnvironment = winsettings.ProxyValues(previous.ListenPort, previous.Username, previous.Password)
		}
	}
	installer := env.Installer(layout, env.Stdout)
	installer.Auth = options.auth
	result, err := installer.Install(link)
	if err != nil {
		return fail(env, err)
	}
	fmt.Fprintf(env.Stdout, i18n.T("Installed sing-box %s. The proxy listens on 127.0.0.1:%d.\n"), result.SingBox, result.ListenPort)
	if result.Auth {
		fmt.Fprintln(env.Stdout, i18n.T("The proxy asks for a password, so other accounts here cannot use it. Shells get it from sbc, and 'sbc env' prints it for scripts."))
	}
	// Unix shells use a prompt hook. Windows settings follow the proxy check.
	if env.OS != "windows" {
		if err := setUpShell(env, layout, options); err != nil {
			return fail(env, err)
		}
	}
	if err := installService(env, layout); err != nil {
		fmt.Fprintf(env.Stderr, i18n.T("sbc: the service could not be set up (%v).\nRun 'sbc run' to start sing-box in the foreground instead.\n"), err)
		return 1
	}
	fmt.Fprintln(env.Stdout, i18n.T("The sbc service is running, and refreshes the config every 6 hours."))
	if err := checkProxy(env, layout, options); err != nil {
		return fail(env, err)
	}
	if env.OS == "windows" {
		if err := env.Windows.Path(layout.CLIDir(), true); err != nil {
			return fail(env, err)
		}
		local, err := readLocal(layout)
		if err != nil {
			return fail(env, err)
		}
		if err := env.Windows.Enable(winsettings.ProxyValues(local.ListenPort, local.Username, local.Password), previousEnvironment); err != nil {
			return fail(env, err)
		}
		windowsEnvironmentNotice(env)
	}
	installNotes(env, result)
	if env.OS == "linux" {
		fmt.Fprintln(env.Stdout, i18n.T("To keep it running after you log out: sudo loginctl enable-linger \"$USER\""))
	}
	return 0
}

// installNotes points out the desktop, which a new install leaves alone, and
// a Docker daemon the bash client pointed at a proxy that now asks for a
// password the daemon does not have.
func installNotes(env Env, result *install.Result) {
	if desk, err := env.Desktop(); err == nil && !result.Auth {
		if state, err := desk.State(result.ListenPort); err == nil && state == desktop.Off {
			fmt.Fprintln(env.Stdout, i18n.T("To point the desktop at the proxy too: sbc desktop on"))
		}
	}
	if daemon, err := env.Docker(); err == nil && result.Auth {
		if state, err := daemon.State(result.ListenPort); err == nil && (state == docker.On || state == docker.Stale) {
			fmt.Fprintln(env.Stderr, i18n.T("sbc: the Docker daemon points at this proxy without its password, so image pulls through it fail. 'sbc docker off' takes the daemon off the proxy, and 'sbc install --auth off' drops the password."))
		}
	}
}

// checkProxy applies the requested route and protocol, then fetches a page
// through the proxy, so an install that cannot reach the server says so.
func checkProxy(env Env, layout paths.Layout, options installOptions) error {
	client, _, err := localAPI(layout)
	if err != nil {
		return err
	}
	if err := client.WaitReady(15 * time.Second); err != nil {
		return i18n.Errorf("sing-box did not start: %w", err)
	}
	if options.route != "" {
		if err := client.SetMode(options.route); err != nil {
			fmt.Fprintf(env.Stderr, i18n.T("sbc: %v; keeping the default route.\n"), err)
		}
	}
	if options.protocol != "" {
		if err := client.Select(api.ProxySelector, options.protocol); err != nil {
			fmt.Fprintf(env.Stderr, i18n.T("sbc: %v; keeping the default protocol.\n"), err)
		}
	}
	route, _, err := client.Modes()
	if err != nil {
		return err
	}
	protocol, _, err := client.Selected(api.ProxySelector)
	if err != nil {
		return err
	}
	delay, err := client.Delay(api.ProxySelector, 10*time.Second)
	if err != nil {
		return i18n.Errorf("sing-box runs, but no page loads through %s: %v.\nTry another protocol with 'sbc protocol <name>', or check this network", protocol, err)
	}
	fmt.Fprintf(env.Stdout, i18n.T("Route %s, protocol %s: a page loads through the proxy in %d ms.\n"), route, protocol, delay.Milliseconds())
	return nil
}

// installService copies sbc beside sing-box, where the services run it from,
// and starts them.
func installService(env Env, layout paths.Layout) error {
	self, err := env.Self()
	if err != nil {
		return err
	}
	if self != layout.SBC() {
		data, err := os.ReadFile(self)
		if err != nil {
			return err
		}
		if err := paths.WriteFile(layout.SBC(), data, 0o755); err != nil {
			return err
		}
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return err
	}
	if err := manager.Install(); err != nil {
		return err
	}
	if tasks, ok := manager.(*service.Tasks); ok && tasks.Interactive {
		fmt.Fprintln(env.Stderr, i18n.T("sbc: this machine does not let tasks run in the background, so sing-box runs in your session: a console window stays open while it runs, and it stops when you sign out."))
	}
	running := manager.Active()
	if err := manager.Start(); err != nil {
		return err
	}
	// A reinstall wrote a new config, which a running sing-box reads only when
	// it restarts.
	if running {
		return manager.Restart()
	}
	return nil
}

func localAPI(layout paths.Layout) (*api.Client, singbox.Local, error) {
	config, err := os.ReadFile(layout.ConfigFile())
	if err != nil {
		return nil, singbox.Local{}, i18n.New("sbc is not installed; run 'sbc install'")
	}
	local, err := singbox.ReadLocal(config)
	if err != nil {
		return nil, singbox.Local{}, err
	}
	return api.New(local.APIPort, local.APISecret), local, nil
}

func runStatus(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	client, local, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	state := i18n.T("not set up")
	if manager, err := env.Service(layout.SBC()); err == nil {
		state = i18n.T("stopped")
		if manager.Active() {
			state = i18n.T("running")
		}
	}
	password := ""
	if local.Username != "" {
		password = i18n.T(", with a password")
	}
	fmt.Fprintf(env.Stdout, i18n.T("service:  %s\nproxy:    127.0.0.1:%d%s\n"), state, local.ListenPort, password)
	shellState := i18n.T("off")
	if _, err := os.Stat(layout.EnvFile()); err == nil {
		shellState = i18n.T("on")
	}
	if env.OS == "windows" {
		shellState, err = env.Windows.State(local.ListenPort, local.Username, local.Password)
		if err != nil {
			return fail(env, err)
		}
	}
	fmt.Fprintf(env.Stdout, i18n.T("shells:   %s\n"), shellState)
	if desk, err := env.Desktop(); err == nil {
		if state, err := desk.State(local.ListenPort); err == nil {
			fmt.Fprintf(env.Stdout, i18n.T("desktop:  %s (%s)\n"), state, desk.Name())
		}
	}
	if daemon, err := env.Docker(); err == nil {
		if state, err := daemon.State(local.ListenPort); err == nil {
			fmt.Fprintf(env.Stdout, i18n.T("docker:   %s\n"), state)
		}
	}
	if usage, at, ok := install.ReadUsage(layout); ok {
		fmt.Fprintf(env.Stdout, i18n.T("traffic:  %s up, %s down this billing cycle, as of %s\n"),
			formatBytes(usage.Upload), formatBytes(usage.Download), at.Format("2006-01-02 15:04"))
	}
	if hint, err := os.ReadFile(layout.UpgradeHint()); err == nil {
		fmt.Fprintf(env.Stdout, i18n.T("upgrade:  %s is available; run 'sbc upgrade'\n"), strings.TrimSpace(string(hint)))
	}
	mode, _, err := client.Modes()
	if err != nil {
		fmt.Fprintf(env.Stdout, i18n.T("sing-box: %v\n"), err)
		return 0
	}
	protocol, _, _ := client.Selected(api.ProxySelector)
	fmt.Fprintf(env.Stdout, i18n.T("route:    %s\nprotocol: %s\n"), mode, protocol)
	return 0
}

func runSwitch(env Env, args []string, what string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	client, _, err := localAPI(layout)
	if err != nil {
		return fail(env, err)
	}
	current, options := "", []string(nil)
	if what == "route" {
		current, options, err = client.Modes()
	} else {
		current, options, err = client.Selected(api.ProxySelector)
	}
	if err != nil {
		return fail(env, err)
	}
	if len(args) == 0 {
		fmt.Fprintf(env.Stdout, i18n.T("%s (choices: %s)\n"), current, strings.Join(options, ", "))
		return 0
	}
	if what == "route" {
		err = client.SetMode(args[0])
	} else {
		err = client.Select(api.ProxySelector, args[0])
	}
	if err != nil {
		return fail(env, err)
	}
	if what == "route" {
		fmt.Fprintf(env.Stdout, i18n.T("Route: %s\n"), args[0])
	} else {
		fmt.Fprintf(env.Stdout, i18n.T("Protocol: %s\n"), args[0])
	}
	return 0
}

func runUpdate(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	link, err := install.ReadLink(layout)
	if err != nil {
		return fail(env, err)
	}
	manager, err := env.Service(layout.SBC())
	if err != nil {
		return fail(env, err)
	}
	// A foreground `sbc run` picks the config up at its next start. A service
	// that runs now restarts, and again after a rollback, when a config that
	// could not start may have left it stopped.
	managed := manager.Active()
	restart := func() error {
		if !managed {
			return nil
		}
		return manager.Restart()
	}
	ready := func() error {
		if !managed {
			return nil
		}
		client, _, err := localAPI(layout)
		if err != nil {
			return err
		}
		return client.WaitReady(10 * time.Second)
	}
	installer := env.Installer(layout, env.Stdout)
	changed, err := installer.Refresh(link, restart, ready)
	if err != nil {
		return fail(env, err)
	}
	if changed {
		fmt.Fprintln(env.Stdout, i18n.T("The config changed and sing-box restarted with it."))
	} else {
		fmt.Fprintln(env.Stdout, i18n.T("The config is up to date."))
	}
	// The hint is a courtesy, so a failed check does not fail the refresh.
	if available, _, err := installer.CheckUpgrade(link, env.Version); err == nil {
		if available.Any() {
			paths.WriteFile(layout.UpgradeHint(), []byte(available.SBC+" "+available.SingBox+"\n"), 0o600)
		} else {
			os.Remove(layout.UpgradeHint())
		}
	}
	return 0
}

func runUninstall(env Env, args []string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	local, localErr := readLocal(layout)
	// Taking the daemon off the proxy restarts Docker, which is worth its own
	// command rather than a side effect of this one.
	if daemon, err := env.Docker(); err == nil && localErr == nil {
		if state, err := daemon.State(local.ListenPort); err == nil && (state == docker.On || state == docker.Stale) {
			return fail(env, i18n.New("the Docker daemon uses the proxy; run 'sbc docker off' first, which restarts Docker"))
		}
	}
	if len(args) == 0 || args[0] != "--yes" {
		fmt.Fprint(env.Stderr, i18n.T("Stop sing-box and remove sbc, its config and its subscription link? [y/N] "))
		answer, _ := bufio.NewReader(env.Stdin).ReadString('\n')
		answer = strings.TrimPrefix(strings.TrimSpace(answer), "\ufeff")
		if strings.ToLower(strings.TrimSpace(answer)) != "y" {
			fmt.Fprintln(env.Stdout, i18n.T("Nothing was removed."))
			return 1
		}
	}
	if env.OS == "windows" && localErr != nil {
		return fail(env, localErr)
	}
	desk, deskErr := env.Desktop()
	if env.OS == "windows" && deskErr != nil {
		return fail(env, deskErr)
	}
	if deskErr == nil && localErr == nil {
		state, err := desk.State(local.ListenPort)
		if env.OS == "windows" && err != nil {
			return fail(env, err)
		}
		if err == nil && state == desktop.On {
			if err := desk.Off(); err != nil {
				return fail(env, err)
			}
		}
	}
	if env.OS == "windows" {
		if err := env.Windows.Move(local.ListenPort, 0, local.Username, local.Password); err != nil {
			return fail(env, err)
		}
		if err := env.Windows.Path(layout.CLIDir(), false); err != nil {
			return fail(env, err)
		}
	}
	if manager, err := env.Service(layout.SBC()); err == nil {
		if err := manager.Remove(); err != nil {
			return fail(env, err)
		}
	}
	if home, err := env.Home(); err == nil && env.OS != "windows" {
		for _, shellName := range []string{"bash", "zsh"} {
			rc, _ := shell.RCFile(home, shellName)
			if err := shell.RemoveBlock(rc); err != nil {
				return fail(env, err)
			}
		}
	}
	dirs := []string{layout.Config}
	if layout.Data != layout.Config {
		dirs = append(dirs, layout.Data)
	}
	for _, dir := range dirs {
		if err := os.RemoveAll(dir); err != nil {
			// Windows refuses to delete a running program, so the directory
			// this sbc runs from goes once it has exited.
			if self, selfErr := env.Self(); env.OS != "windows" || selfErr != nil || !within(self, dir) {
				return fail(env, err)
			}
			if err := deleteWhenExited(dir); err != nil {
				return fail(env, err)
			}
		}
	}
	fmt.Fprintln(env.Stdout, i18n.T("sbc is removed."))
	return 0
}

// within reports whether path is inside dir.
func within(path, dir string) bool {
	rel, err := filepath.Rel(dir, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// logLimit is the size at which the log sbc keeps itself starts over.
const logLimit = 10 << 20

func runSingBox(env Env) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	binary := layout.SingBox()
	args := []string{binary, "run", "-D", layout.Data, "-c", layout.ConfigFile()}
	output := env.Stdout
	if env.OS == "windows" {
		// A program moved aside by an upgrade is free once it has exited.
		os.Remove(binary + ".old")
		os.Remove(layout.SBC() + ".old")
		if !isTerminal(env.Stdout) {
			// A scheduled task has no console, so the log file keeps the
			// output.
			log, err := openLog(layout.Log())
			if err != nil {
				return fail(env, err)
			}
			defer log.Close()
			output = log
		}
	}
	if err := env.Exec(binary, args, output, layout.PIDFile()); err != nil {
		return fail(env, i18n.Errorf("start sing-box: %w; run 'sbc install' first", err))
	}
	return 0
}

// openLog opens the log for appending, starting over once it is large.
func openLog(path string) (*os.File, error) {
	flags := os.O_CREATE | os.O_WRONLY | os.O_APPEND
	if info, err := os.Stat(path); err == nil && info.Size() > logLimit {
		flags |= os.O_TRUNC
	}
	return os.OpenFile(path, flags, 0o600)
}
