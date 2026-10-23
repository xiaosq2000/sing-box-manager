package counter

import (
	"context"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
)

func TestQueryPreservesCountersAndSeparatesWireIdentities(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	server.RegisterService(&grpc.ServiceDesc{
		ServiceName: "v2ray.core.app.stats.command.StatsService",
		HandlerType: (*interface{})(nil),
		Methods: []grpc.MethodDesc{{MethodName: "QueryStats", Handler: func(_ any, ctx context.Context, decode func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
			request := &QueryStatsRequest{}
			if err := decode(request); err != nil {
				return nil, err
			}
			if request.Reset_ || request.Regexp || len(request.Patterns) != 1 || request.Patterns[0] != "user>>>trojan:alice>>>traffic>>>" {
				t.Error("query changed counter semantics or identity")
			}
			return &QueryStatsResponse{Stat: []*Stat{
				{Name: "user>>>trojan:alice>>>traffic>>>uplink", Value: 10},
				{Name: "user>>>trojan:alice>>>traffic>>>downlink", Value: 20},
				{Name: "user>>>snell:alice>>>traffic>>>uplink", Value: 900},
				{Name: "uplink", Value: 800},
			}}, nil
		}}},
	}, &struct{}{})
	go server.Serve(listener)
	defer server.Stop()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	up, down, err := Read(ctx, listener.Addr().String(), "trojan:alice")
	if err != nil || up != 10 || down != 20 {
		t.Fatalf("unexpected query: %d %d %v", up, down, err)
	}
}

func TestCounterRejectsNonLoopbackAddresses(t *testing.T) {
	for _, address := range []string{"localhost:19080", "192.0.2.1:19080", "[::]:19080", "invalid"} {
		if _, _, err := Read(context.Background(), address, "trojan:alice"); err == nil {
			t.Errorf("accepted %q", address)
		}
	}
}
