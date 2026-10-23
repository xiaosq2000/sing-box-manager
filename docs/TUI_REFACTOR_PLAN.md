# sbc terminal UI design and acceptance

Checked on 2026-10-23.

The implementation follows this bounded design. Local fixture checks pass.
Native font and platform acceptance remain open. Follow the
[terminal UI design requirements](DEVELOPMENT.md#terminal-ui-design).

## Scope

Keep Cobra, Lipgloss tables, Bubble Tea models, Bubbles spinner, CJK widths,
safe display labels, stacked layouts, inline progress and complete final reports.
Keep command syntax, business operations, authentication, operation locks, TUN
guards and machine-readable schemas unchanged.

Use upstream v2 modules without a local fork. `go.mod` pins Bubble Tea v2.0.8,
Bubbles v2.2.1 and Lipgloss v2.0.6. The dependency graph supports Go 1.25.
Do not select Bubble Tea v2.1.0 without a separate toolchain change. It requires
Go 1.26.

Do not expand this work into a client rewrite, generic widget framework,
full-screen dashboard, theme configuration layer or font detector.
Mouse control, images, hyperlinks and clipboard integration remain outside scope.

## Output ownership

| Component                        | Responsibility                                                                                                     |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `cli.Run`                        | Own one lazy human-output context and close it before returning                                                    |
| `cli.Env.output`                 | Reuse the context for the command's stdout. Machine output keeps the original writer                               |
| `cli.withPanel`                  | Collect plain text in a separate buffer context, then style the complete panel once                                |
| `tui.Output`                     | Snapshot terminal-related environment values, detect capabilities, bind the stream and own output-mode restoration |
| `tui.NewTheme(profile)`          | Build pure ANSI semantic styles without renderer state or background detection                                     |
| Panel, table and line formatters | Accept explicit themes and widths. Do not detect terminal capabilities                                             |
| Selector and live-table models   | Own only view data, dimensions, navigation and interaction state                                                   |
| `tui.Program`                    | Run Bubble Tea, report write failures and restore saved terminal modes, including startup-error paths              |

Lipgloss v2 styles do not contain renderers. Default styles use only basic ANSI
slots, so static output needs no RGB conversion or global writer configuration.
An unstyled profile creates no SGR attributes, including bold.

Help, panels, lists and live/final reports share the same effective policy.
Static final reports read the current width. Interactive models receive resize
messages. Missing values from an injected environment never inherit process
values implicitly.

## Theme and glyph policy

| Role or condition                  | Behavior                                                                                 |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| Body, muted text, borders and help | Inherit foreground and background                                                        |
| Headings and keys                  | Default foreground with bold when the profile permits styling                            |
| Focus / primary                    | ANSI slot 6. Focus also has an ordinary `>` marker                                       |
| Success / warning / danger         | ANSI slots 2 / 3 / 1, with status text                                                   |
| Truecolor / 256-color / 16-color   | Use the same ANSI slots, not fixed RGB                                                   |
| Non-empty `NO_COLOR`               | Remove all SGR attributes. Keep keyboard interaction, animation and scrolling            |
| `CLICOLOR=0`                       | Remove styling unless `CLICOLOR_FORCE` overrides it. `NO_COLOR` still takes precedence   |
| Files, pipes or `TERM=dumb`        | Static output without UI controls, animation or terminal queries, even with forced color |
| Non-TTY input, TTY output          | Static choices. Do not reopen `/dev/tty` for interaction                                 |
| JSON, shell exports and completion | Keep the original output path without UI initialization or terminal queries              |

The spinner uses `spinner.Line` with `|`, `/`, `-` and backslash frames.
Key controls need no Nerd Font, private-use icons or emoji.
Common Unicode borders remain in use. Native baseline-font acceptance is still
required. Do not infer glyph coverage from a PTY or truecolor support.

## Interaction and lifecycle

- Keep inline views. Do not enter the alternate screen or change terminal colors.
- Use up/down to change the focused item. Use Page Up/Down to read oversized items.
- Submit only with Enter. Space does not submit. Esc or `q` cancels a selector.
- Keep Ctrl-C/SIGINT exit 130 and POSIX SIGTERM exit 143, with partial diagnostic results.
- Preserve model data through resize. Below four columns, display the `<>` resize
  hint, or `<` in one column, until the window can contain the view.
- Cancel diagnostic workers when the UI fails. Print the complete final report once.
- Stop the UI on a write failure. Attempt cleanup and return failure instead of success.
- Restore Windows output mode after static output as well as interactive output.

`programWriter` implements the complete `term.File` interface, not only `Fd`.
Bubble Tea needs this interface to detect terminal dimensions and console modes.
The writer records output errors without owning the inherited stdio files.

Bubble Tea starts its renderer before model `Init`. The wrapper observes this
boundary. Its renderer-stop channel has no receiver before renderer startup.
Do not call `Kill` on that early failure path. Restore the saved input/output
modes directly instead. Cleanup after renderer startup still uses Bubble Tea.
A damaged stream can prevent cleanup. Report restoration and write errors.

## Checks and remaining acceptance

Local checks use Go 1.25.8 with cgo disabled. No live installation, deployment,
proxy operation or host setting change is part of these checks.

| Check                                                                                 | Result                                           |
| ------------------------------------------------------------------------------------- | ------------------------------------------------ |
| Full Go suite with `-count=1`                                                         | Pass                                             |
| Linux `go vet` and Windows cross-platform `go vet`                                    | Pass                                             |
| Builds for Linux amd64/arm64, macOS amd64/arm64 and Windows amd64                     | Pass                                             |
| Exact ANSI roles under explicit truecolor/256/16/no-color profiles                    | Pass                                             |
| Foreground PTY startup bytes for help, JSON, shell integration and completion         | Pass, without background/cursor-position queries |
| Forced-color fallbacks, injected environment, no-color keyboard interaction           | Pass                                             |
| Long-item paging, narrow/resize/CJK layouts and ASCII control glyphs                  | Pass                                             |
| Startup failure after raw mode, output failure, cancellation and terminal restoration | Pass in Linux fixtures                           |
| Ubuntu 18.04 default GNOME Terminal and default monospace font, without Nerd Fonts    | Pending native acceptance                        |
| Real custom ANSI palettes, light/dark themes and actual glyph rendering               | Pending native acceptance                        |
| Native macOS Terminal and Windows Terminal/traditional console, PowerShell 5.1/7      | Pending native acceptance                        |

PTY success does not prove font coverage. Cross-compilation does not prove native
terminal behavior. Use synthetic data and authorized disposable environments for
native checks. Record the terminal and font versions.

Run the focused and full checks through the existing Pixi environment:

```sh
pixi run go test ./internal/tui ./internal/cli -count=1
pixi run go test ./... -count=1
pixi run lint-go
pixi run docstale check
```

Use existing caches for offline checks. Do not download dependencies through the
metered proxy. See [Development](DEVELOPMENT.md#checks) for check selection.
