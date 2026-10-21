package tun

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/xiaosq2000/sing-box-manager/internal/api"
	"github.com/xiaosq2000/sing-box-manager/internal/i18n"
)

const directProbeURL = "https://www.baidu.com/"

// probePlan keeps activation on the selected policy. Non-direct policies also
// need a working selected proxy. No probe visits an AI provider.
func probePlan(mode string) (string, bool, error) {
	switch mode {
	case "direct":
		return directProbeURL, false, nil
	case "ai-only", "gfw-proxy", "china-direct":
		return directProbeURL, true, nil
	case "global":
		return api.TestURL, true, nil
	default:
		return "", false, i18n.Errorf("unsupported TUN route mode %q", mode)
	}
}

// Verify accepts an activation only when the service, capture, DNS and an
// egress request all work.
func Verify(paths Paths, runner Runner, tools Tools) error {
	if err := waitActive(runner, tools); err != nil {
		return err
	}
	if err := stableProcess(runner, tools); err != nil {
		return err
	}
	if err := waitInterface(runner, tools); err != nil {
		return err
	}
	if err := waitRoutes(runner, tools); err != nil {
		return err
	}
	if _, err := runner.Output(tools.IP, "rule", "show"); err != nil {
		return i18n.New("the TUN routing rules are missing")
	}
	if _, err := runner.Output(tools.Nft, "list", "ruleset"); err != nil {
		return i18n.New("the nftables ruleset is unavailable")
	}
	port, secret, err := ReadAPI(paths.Config)
	if err != nil {
		return err
	}
	tag, err := ReadTunTag(paths.Config)
	if err != nil {
		return err
	}
	client := api.New(port, secret)
	if err := client.WaitReady(15 * time.Second); err != nil {
		return i18n.Errorf("the TUN API did not become ready: %w", err)
	}
	mode, _, err := client.Modes()
	if err != nil {
		return err
	}
	page, proxyRequired, err := probePlan(mode)
	if err != nil {
		return err
	}
	if proxyRequired {
		if _, err := client.Delay(api.ProxySelector, 10*time.Second); err != nil {
			return i18n.Errorf("the selected proxy is unavailable in TUN mode %s: %w", mode, err)
		}
	}
	if err := verifyRequest(client, tag, page); err != nil {
		return err
	}
	current, _, err := client.Modes()
	if err != nil || current != mode {
		return i18n.New("the TUN route mode changed during verification")
	}
	return nil
}

func waitActive(runner Runner, tools Tools) error {
	for range 30 {
		if err := runner.Run(tools.Systemctl, "is-active", "--quiet", ServiceName); err == nil {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return i18n.New("the TUN service did not become active")
}

// waitInterface waits for sing-box, which systemd reports active before it
// creates the interface.
func waitInterface(runner Runner, tools Tools) error {
	deadline := time.Now().Add(15 * time.Second)
	for {
		if _, err := runner.Output(tools.IP, "-o", "link", "show", InterfaceName); err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return i18n.Errorf("the %s interface is missing", InterfaceName)
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func waitRoutes(runner Runner, tools Tools) error {
	for _, family := range []string{"-4", "-6"} {
		deadline := time.Now().Add(10 * time.Second)
		for {
			output, err := runner.Output(tools.IP, family, "route", "show", "dev", InterfaceName)
			if err == nil && strings.TrimSpace(output) != "" {
				break
			}
			if time.Now().After(deadline) {
				return i18n.Errorf("the %s interface has no %s routes", InterfaceName, family)
			}
			time.Sleep(200 * time.Millisecond)
		}
	}
	return nil
}

func stableProcess(runner Runner, tools Tools) error {
	first, err := mainPID(runner, tools)
	if err != nil {
		return err
	}
	time.Sleep(time.Second)
	second, err := mainPID(runner, tools)
	if err != nil {
		return err
	}
	if first == 0 || first != second {
		return i18n.New("the TUN service process is not stable")
	}
	return nil
}

func mainPID(runner Runner, tools Tools) (int, error) {
	output, err := runner.Output(tools.Systemctl, "show", "-p", "MainPID", "--value", ServiceName)
	if err != nil {
		return 0, i18n.New("the TUN service has no main process")
	}
	pid, err := strconv.Atoi(strings.TrimSpace(output))
	if err != nil || pid <= 0 {
		return 0, i18n.New("the TUN service has no main process")
	}
	return pid, nil
}

// verifyRequest proves capture and an HTTP response on the same socket. DNS
// uses the application's system resolver. The transport ignores proxy variables,
// never redirects, and validates HTTPS with the system trust store.
func verifyRequest(client *api.Client, tag, page string) error {
	target, err := url.Parse(page)
	if err != nil {
		return err
	}
	port := target.Port()
	if port == "" {
		port = "443"
		if target.Scheme == "http" {
			port = "80"
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	resolver := &net.Resolver{PreferGo: true}
	addresses, err := resolver.LookupHost(ctx, target.Hostname())
	if err != nil {
		return i18n.Errorf("DNS through the TUN failed: %w", err)
	}
	if len(addresses) == 0 {
		return i18n.New("DNS through the TUN returned no address")
	}
	host := addresses[0]
	for _, address := range addresses {
		if ip := net.ParseIP(address); ip != nil && ip.To4() != nil {
			host = address
			break
		}
	}
	before, err := connectionIDs(client)
	if err != nil {
		return err
	}
	var dialer net.Dialer
	probe, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(host, port))
	if err != nil {
		return i18n.Errorf("the TUN test connection failed: %w", err)
	}
	defer probe.Close()
	localHost, localPort, err := net.SplitHostPort(probe.LocalAddr().String())
	if err != nil {
		return err
	}
	deadline := time.Now().Add(5 * time.Second)
	observed := false
	for !observed {
		connections, err := client.Connections()
		if err != nil {
			return err
		}
		for _, connection := range connections {
			if captured(connection, tag, localHost, localPort, host, port, before) {
				observed = true
				break
			}
		}
		if !observed {
			if time.Now().After(deadline) {
				return i18n.New("the TUN did not capture the test socket")
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	used := false
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true, TLSHandshakeTimeout: 5 * time.Second,
		DialContext: func(context.Context, string, string) (net.Conn, error) {
			if used {
				return nil, fmt.Errorf("TUN probe refused a second connection")
			}
			used = true
			return probe, nil
		},
	}
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Transport: transport, Timeout: 10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, page, nil)
	if err != nil {
		return err
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return i18n.Errorf("the captured TUN request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 400 {
		return i18n.Errorf("the captured TUN request returned HTTP %d", response.StatusCode)
	}
	_, err = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
	return err
}

func connectionIDs(client *api.Client) (map[string]struct{}, error) {
	connections, err := client.Connections()
	if err != nil {
		return nil, err
	}
	ids := make(map[string]struct{}, len(connections))
	for _, connection := range connections {
		ids[connection.ID] = struct{}{}
	}
	return ids, nil
}

func captured(connection api.Connection, tag, sourceHost, sourcePort, host, port string, before map[string]struct{}) bool {
	if _, seen := before[connection.ID]; seen {
		return false
	}
	metadata := connection.Metadata
	return metadata.Type == "tun/"+tag && metadata.Network == "tcp" &&
		metadata.SourceIP == sourceHost && metadata.SourcePort == sourcePort &&
		metadata.DestinationIP == host && metadata.DestinationPort == port
}
