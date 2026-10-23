import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Outlet, createRootRouteWithContext } from '@tanstack/react-router';
import { StudioProvider } from '@/components/studio-state';

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: () => {
    const queryClient = Route.useRouteContext().queryClient;
    return <QueryClientProvider client={queryClient}><StudioProvider><Outlet /></StudioProvider></QueryClientProvider>;
  },
});
