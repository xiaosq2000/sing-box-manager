package counter

import (
	"context"
	"errors"
	"net"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

var (
	ErrDeadline    = errors.New("counter API deadline exceeded")
	ErrCanceled    = errors.New("counter API request canceled")
	ErrUnavailable = errors.New("counter API unavailable")
	ErrRejected    = errors.New("counter API request rejected")
)

func Read(ctx context.Context, address, wireName string) (upload, download int64, err error) {
	host, _, err := net.SplitHostPort(address)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return 0, 0, errors.New("counter address must be a loopback IP")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(1<<20)))
	if err != nil {
		return 0, 0, errors.New("counter connection failed")
	}
	defer conn.Close()
	prefix := "user>>>" + wireName + ">>>traffic>>>"
	response := &QueryStatsResponse{}
	if err := conn.Invoke(ctx, "/v2ray.core.app.stats.command.StatsService/QueryStats", &QueryStatsRequest{Patterns: []string{prefix}}, response, grpc.WaitForReady(true)); err != nil {
		return 0, 0, queryError(err)
	}
	for _, stat := range response.Stat {
		if !strings.HasPrefix(stat.Name, prefix) {
			continue
		}
		if stat.Value < 0 {
			return 0, 0, errors.New("negative counter")
		}
		switch strings.TrimPrefix(stat.Name, prefix) {
		case "uplink":
			upload = stat.Value
		case "downlink":
			download = stat.Value
		}
	}
	return upload, download, nil
}

func queryError(err error) error {
	switch status.Code(err) {
	case codes.DeadlineExceeded:
		return ErrDeadline
	case codes.Canceled:
		return ErrCanceled
	case codes.Unavailable:
		return ErrUnavailable
	case codes.PermissionDenied, codes.Unauthenticated:
		return ErrRejected
	default:
		return errors.New("counter query failed")
	}
}
