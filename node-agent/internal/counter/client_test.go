package counter

import (
	"context"
	"errors"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func statsServer(t *testing.T, query func(context.Context, *QueryStatsRequest) (*QueryStatsResponse, error)) *grpc.Server {
	t.Helper()
	server := grpc.NewServer()
	server.RegisterService(&grpc.ServiceDesc{
		ServiceName: "v2ray.core.app.stats.command.StatsService",
		HandlerType: (*interface{})(nil),
		Methods: []grpc.MethodDesc{{MethodName: "QueryStats", Handler: func(_ any, ctx context.Context, decode func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
			request := &QueryStatsRequest{}
			if err := decode(request); err != nil {
				return nil, err
			}
			return query(ctx, request)
		}}},
	}, &struct{}{})
	t.Cleanup(server.Stop)
	return server
}

func serveStats(t *testing.T, query func(context.Context, *QueryStatsRequest) (*QueryStatsResponse, error)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := statsServer(t, query)
	go server.Serve(listener)
	return listener.Addr().String()
}

func closedAddress(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	return address
}

func TestQueryPreservesCountersAndSeparatesWireIdentities(t *testing.T) {
	address := serveStats(t, func(_ context.Context, request *QueryStatsRequest) (*QueryStatsResponse, error) {
		if request.Reset_ || request.Regexp || len(request.Patterns) != 1 || request.Patterns[0] != "user>>>trojan:alice>>>traffic>>>" {
			t.Error("query changed counter semantics or identity")
		}
		return &QueryStatsResponse{Stat: []*Stat{
			{Name: "user>>>trojan:alice>>>traffic>>>uplink", Value: 10},
			{Name: "user>>>trojan:alice>>>traffic>>>downlink", Value: 20},
			{Name: "user>>>snell:alice>>>traffic>>>uplink", Value: 900},
			{Name: "uplink", Value: 800},
		}}, nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	up, down, err := Read(ctx, address, "trojan:alice")
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

func TestCounterWaitsForDelayedListener(t *testing.T) {
	address := closedAddress(t)
	server := statsServer(t, func(context.Context, *QueryStatsRequest) (*QueryStatsResponse, error) {
		return &QueryStatsResponse{Stat: []*Stat{{Name: "user>>>trojan:alice>>>traffic>>>downlink", Value: 20}}}, nil
	})
	started := make(chan error, 1)
	go func() {
		time.Sleep(50 * time.Millisecond)
		listener, err := net.Listen("tcp", address)
		started <- err
		if err == nil {
			defer listener.Close()
			server.Serve(listener)
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	up, down, err := Read(ctx, address, "trojan:alice")
	if startErr := <-started; startErr != nil {
		t.Fatal(startErr)
	}
	if err != nil || up != 0 || down != 20 {
		t.Fatalf("delayed listener: %d %d %v", up, down, err)
	}
}

func TestCounterUnavailableHonorsCallerDeadlineAndCancellation(t *testing.T) {
	address := closedAddress(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, _, err := Read(ctx, address, "trojan:alice"); !errors.Is(err, ErrDeadline) {
		t.Fatalf("expected bounded readiness failure, got %v", err)
	}
	ctx, stop := context.WithCancel(context.Background())
	stop()
	if _, _, err := Read(ctx, address, "trojan:alice"); !errors.Is(err, ErrCanceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
}

func TestCounterBoundsRequestAndPreservesShorterDeadline(t *testing.T) {
	for _, limit := range []time.Duration{5 * time.Second, 250 * time.Millisecond} {
		address := serveStats(t, func(ctx context.Context, _ *QueryStatsRequest) (*QueryStatsResponse, error) {
			deadline, ok := ctx.Deadline()
			if !ok || time.Until(deadline) > limit+100*time.Millisecond {
				t.Error("counter request lost its deadline bound")
			}
			return &QueryStatsResponse{}, nil
		})
		ctx := context.Background()
		cancel := func() {}
		if limit < 5*time.Second {
			ctx, cancel = context.WithTimeout(ctx, limit)
		}
		_, _, err := Read(ctx, address, "trojan:alice")
		cancel()
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestCounterServiceErrorsAreSafeAndAreNotRetried(t *testing.T) {
	for _, test := range []struct {
		code codes.Code
		want string
	}{
		{codes.Unavailable, ErrUnavailable.Error()},
		{codes.DeadlineExceeded, ErrDeadline.Error()},
		{codes.Canceled, ErrCanceled.Error()},
		{codes.PermissionDenied, ErrRejected.Error()},
		{codes.Unauthenticated, ErrRejected.Error()},
		{codes.Internal, "counter query failed"},
	} {
		t.Run(test.code.String(), func(t *testing.T) {
			var calls atomic.Int32
			address := serveStats(t, func(context.Context, *QueryStatsRequest) (*QueryStatsResponse, error) {
				calls.Add(1)
				return nil, status.Error(test.code, "PRIVATE-fixture-credential-and-path")
			})
			_, _, err := Read(context.Background(), address, "trojan:alice")
			if err == nil || err.Error() != test.want || strings.Contains(err.Error(), "PRIVATE") {
				t.Fatalf("unsafe or wrong error class: %v", err)
			}
			if calls.Load() != 1 {
				t.Fatalf("service rejection retried %d times", calls.Load())
			}
		})
	}
}
