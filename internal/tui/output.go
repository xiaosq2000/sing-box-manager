package tui

import (
	"errors"
	"io"
	"strings"
	"sync"
	"sync/atomic"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/colorprofile"
	"github.com/charmbracelet/x/term"
)

// Output owns the human-output policy and terminal resources for one stream.
// Construct it lazily. Machine-readable paths must keep their original writer.
type Output struct {
	Theme       Theme
	in          io.Reader
	out         io.Writer
	environ     []string
	interactive bool
	prepareOnce sync.Once
	prepareErr  error
	restore     func() error
	err         error
}

// NewOutput detects capabilities without querying colors or changing modes.
func NewOutput(in io.Reader, out io.Writer, getenv func(string) string) *Output {
	if out == nil {
		out = io.Discard
	}
	environ := terminalEnvironment(getenv)
	get := environ.getenv
	profile := colorprofile.NoTTY
	if IsTerminal(out) && get("TERM") != "dumb" {
		profile = colorprofile.ASCII
		force := get("CLICOLOR_FORCE") != "" && get("CLICOLOR_FORCE") != "0"
		if get("NO_COLOR") == "" && (get("CLICOLOR") != "0" || force) {
			profile = colorprofile.Detect(out, environ)
			if force && profile < colorprofile.ANSI {
				profile = colorprofile.ANSI
			}
		}
	}
	return &Output{Theme: NewTheme(profile), in: in, out: out, environ: environ,
		interactive: Interactive(in, out, get)}
}

// Only terminal-related variables reach the renderer. An injected getter never
// falls back to the process environment for missing keys.
type terminalEnv []string

func (env terminalEnv) getenv(key string) string {
	for _, entry := range env {
		if value, ok := strings.CutPrefix(entry, key+"="); ok {
			return value
		}
	}
	return ""
}

func terminalEnvironment(getenv func(string) string) terminalEnv {
	keys := []string{"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
		"NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "CI", "ConEmuANSI", "ANSICON", "ANSICON_VER",
		"WT_SESSION", "TMUX", "GOOGLE_CLOUD_SHELL"}
	environ := make(terminalEnv, 0, len(keys))
	for _, key := range keys {
		if value := lookup(getenv, key); value != "" {
			environ = append(environ, key+"="+value)
		}
	}
	return environ
}

func (o *Output) Interactive() bool { return o.interactive }

// Size reads the current size for static final output, not a cached frame size.
func (o *Output) Size() (int, int) { return TerminalSize(o.out) }

func (o *Output) prepare() error {
	o.prepareOnce.Do(func() {
		if o.Theme.Styled() || o.interactive {
			o.restore, o.prepareErr = prepareOutput(o.out)
		}
	})
	return o.prepareErr
}

func (o *Output) Write(p []byte) (int, error) {
	if err := o.prepare(); err != nil {
		return 0, err
	}
	n, err := o.out.Write(p)
	if err == nil && n < len(p) {
		err = io.ErrShortWrite
	}
	if err != nil && o.err == nil {
		o.err = err
	}
	return n, err
}

// Close restores any output mode acquired before static or interactive output.
func (o *Output) Close() error {
	if o.restore != nil {
		o.err = errors.Join(o.err, o.restore())
		o.restore = nil
	}
	return errors.Join(o.prepareErr, o.err)
}

// Program uses the same environment and color policy as static output.
func (o *Output) Program(model tea.Model) (*Program, error) {
	if !o.interactive {
		return nil, errors.New("interactive terminal required")
	}
	if err := o.prepare(); err != nil {
		return nil, err
	}
	program := &Program{output: o}
	program.Program = tea.NewProgram(startupModel{Model: model, started: &program.started},
		tea.WithInput(o.in), tea.WithOutput(programWriter{Output: o, stop: func() {
			program.writeFailed.Do(func() { go program.Quit() })
		}}), tea.WithEnvironment(o.environ),
		tea.WithColorProfile(o.Theme.profile), tea.WithoutSignalHandler())
	return program, nil
}

// programWriter retains the real terminal descriptor and records write errors.
type programWriter struct {
	*Output
	stop func()
}

var _ term.File = programWriter{}

func (w programWriter) Fd() uintptr {
	return w.out.(interface{ Fd() uintptr }).Fd()
}

func (w programWriter) Read(p []byte) (int, error) { return w.out.(io.Reader).Read(p) }

func (w programWriter) Write(p []byte) (int, error) {
	n, err := w.Output.Write(p)
	if err != nil && w.stop != nil {
		w.stop()
	}
	return n, err
}

// The command owns terminal state, not the lifetime of inherited stdio files.
func (w programWriter) Close() error { return nil }

// Program adds startup cleanup and write-error reporting to Bubble Tea.
type Program struct {
	*tea.Program
	output      *Output
	started     atomic.Bool
	writeFailed sync.Once
}

// Bubble Tea starts its renderer before calling Init. Kill cannot be used on
// earlier startup failures because its renderer-stop channel has no receiver.
type startupModel struct {
	tea.Model
	started *atomic.Bool
}

func (m startupModel) Init() tea.Cmd {
	m.started.Store(true)
	return m.Model.Init()
}

func (p *Program) Run() (model tea.Model, err error) {
	states, err := captureTerminalStates(p.output.in, p.output.out)
	if err != nil {
		p.Program.Kill() // No renderer exists before Run starts.
		return nil, err
	}
	defer func() {
		if p.started.Load() {
			p.Program.Kill()
		}
		for _, saved := range states {
			err = errors.Join(err, term.Restore(saved.fd, saved.state))
		}
		err = errors.Join(err, p.output.err)
	}()
	model, err = p.Program.Run()
	if wrapped, ok := model.(startupModel); ok {
		model = wrapped.Model
	}
	return model, err
}

type terminalState struct {
	fd    uintptr
	state *term.State
}

func captureTerminalStates(streams ...any) ([]terminalState, error) {
	var states []terminalState
	for _, stream := range streams {
		if file, ok := stream.(term.File); ok && term.IsTerminal(file.Fd()) {
			fd := file.Fd()
			state, err := term.GetState(fd)
			if err != nil {
				return nil, err
			}
			states = append(states, terminalState{fd: fd, state: state})
		}
	}
	return states, nil
}
