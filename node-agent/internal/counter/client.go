package counter

import (
	"context"
	"errors"
	"net"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func Read(ctx context.Context, address, wireName string) (upload, download int64, err error) {
	host, _, err := net.SplitHostPort(address)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return 0, 0, errors.New("counter address must be a loopback IP")
	}
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(1<<20)))
	if err != nil {
		return 0, 0, errors.New("counter connection failed")
	}
	defer conn.Close()
	prefix := "user>>>" + wireName + ">>>traffic>>>"
	response := &QueryStatsResponse{}
	if err := conn.Invoke(ctx, "/v2ray.core.app.stats.command.StatsService/QueryStats", &QueryStatsRequest{Patterns: []string{prefix}}, response); err != nil {
		return 0, 0, errors.New("counter query failed")
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
