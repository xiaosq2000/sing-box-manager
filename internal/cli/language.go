package cli

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
	"github.com/xiaosq2000/sing-box-manager/internal/paths"
)

const (
	languageAuto    = "auto"
	languageEnglish = "en"
	languageChinese = "zh"
)

// ConfigureLanguage applies the saved CLI language before the command tree is
// built. A missing layout leaves SBC_LANG and the locale in charge, as help
// works without an installation. A broken saved setting falls back to them and
// returns the error.
func ConfigureLanguage() error {
	layout, err := paths.Default()
	if err != nil {
		return nil
	}
	return applyLanguage(layout)
}

func applyLanguage(layout paths.Layout) error {
	saved, err := readLanguage(layout.LanguageFile())
	if err != nil {
		i18n.Use(os.Getenv, languageAuto)
		return err
	}
	i18n.Use(os.Getenv, saved)
	return nil
}

// readLanguage returns the saved choice, or auto when no setting exists.
func readLanguage(path string) (string, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return languageAuto, nil
	}
	if err != nil {
		return languageAuto, i18n.Errorf("could not read the saved CLI language: %w", err)
	}
	choice := strings.TrimSpace(strings.TrimPrefix(string(data), "\ufeff"))
	switch choice {
	case "", languageAuto:
		return languageAuto, nil
	case languageEnglish, languageChinese:
		return choice, nil
	default:
		return languageAuto, i18n.Errorf("the saved CLI language %q is invalid; run 'sbc lang auto' or set 'sbc lang en|zh'", choice)
	}
}

func writeLanguage(path, choice string) error {
	switch choice {
	case languageAuto:
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return i18n.Errorf("could not save the CLI language: %w", err)
		}
		return nil
	case languageEnglish, languageChinese:
		if err := paths.WriteFile(path, []byte(choice+"\n"), 0o600); err != nil {
			return i18n.Errorf("could not save the CLI language: %w", err)
		}
		return nil
	default:
		return i18n.Errorf("unknown CLI language %q", choice)
	}
}

func runLang(env Env, args []string) int {
	layout, err := env.Layout()
	if err != nil {
		return fail(env, err)
	}
	path := layout.LanguageFile()
	saved, loadErr := readLanguage(path)
	if len(args) == 0 {
		if loadErr != nil {
			return fail(env, loadErr)
		}
		chinese, source := i18n.Resolve(os.Getenv, saved)
		i18n.SetChinese(chinese)
		fmt.Fprintf(env.Stdout, i18n.T("Language: %s (%s)\n"), languageName(chinese), languageSource(source))
		if saved != languageAuto && source != i18n.SourceSetting {
			savedChinese := saved == languageChinese
			if savedChinese != chinese {
				fmt.Fprintf(env.Stdout, i18n.T("Saved choice: %s\n"), languageName(savedChinese))
			}
		}
		return 0
	}

	choice := args[0]
	if err := writeLanguage(path, choice); err != nil {
		return fail(env, err)
	}
	source := i18n.Use(os.Getenv, choice)
	if choice == languageAuto {
		if source == i18n.SourceEnvironment {
			fmt.Fprintln(env.Stdout, i18n.T("The saved CLI language is cleared, but SBC_LANG still selects the language."))
		} else {
			fmt.Fprintln(env.Stdout, i18n.T("The CLI language follows the locale; no saved choice remains."))
		}
		return 0
	}
	chinese := choice == languageChinese
	if source == i18n.SourceEnvironment && i18n.Chinese(os.Getenv) != chinese {
		fmt.Fprintf(env.Stdout, i18n.T("Saved %s, but SBC_LANG currently overrides it.\n"), languageName(chinese))
	} else {
		fmt.Fprintf(env.Stdout, i18n.T("CLI language set to %s.\n"), languageName(chinese))
	}
	return 0
}

func languageName(chinese bool) string {
	if chinese {
		return i18n.Pick("Simplified Chinese", "简体中文")
	}
	return i18n.Pick("English", "英文")
}

func languageSource(source i18n.Source) string {
	switch source {
	case i18n.SourceEnvironment:
		return "SBC_LANG"
	case i18n.SourceSetting:
		return i18n.T("saved setting")
	case i18n.SourceLocale:
		return i18n.T("locale")
	default:
		return i18n.T("default")
	}
}
