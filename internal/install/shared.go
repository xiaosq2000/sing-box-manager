package install

import (
	"bufio"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// nobody is the overflow account NFS maps unknown users to.
const nobody = 65534

// firstPersonUID is the lowest UID a person's account gets. Lower UIDs belong
// to the system.
func firstPersonUID(goos string) int {
	if goos == "darwin" {
		return 501
	}
	return 1000
}

// SharedHost reports whether other people log in to this machine: another
// account with a login shell in /etc/passwd, or a home directory beside this
// one that belongs to someone else, which is how accounts from LDAP show up.
func SharedHost(goos string) bool {
	uid, least := os.Getuid(), firstPersonUID(goos)
	if passwd, err := os.Open("/etc/passwd"); err == nil {
		defer passwd.Close()
		if otherAccount(passwd, uid, least) {
			return true
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return false
	}
	return otherHome(filepath.Dir(home), uid, least, fileOwner)
}

func otherAccount(passwd io.Reader, uid, least int) bool {
	scanner := bufio.NewScanner(passwd)
	for scanner.Scan() {
		fields := strings.Split(scanner.Text(), ":")
		if len(fields) < 7 {
			continue
		}
		id, err := strconv.Atoi(fields[2])
		if err != nil || id == uid || id < least || id == nobody {
			continue
		}
		shell := fields[6]
		if strings.HasSuffix(shell, "nologin") || strings.HasSuffix(shell, "/false") {
			continue
		}
		return true
	}
	return false
}

// otherHome stops at the first directory someone else owns, so a cluster's
// /home with thousands of entries costs little.
func otherHome(dir string, uid, least int, owner func(fs.FileInfo) (int, bool)) bool {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		if id, ok := owner(info); ok && id != uid && id >= least && id != nobody {
			return true
		}
	}
	return false
}
