import { createFileRoute } from '@tanstack/react-router';
import { StudioShell } from '@/components/studio-ui';
import { JobsScreen } from '@/components/studio-workspaces';
export const Route = createFileRoute('/jobs')({ component: () => <StudioShell><JobsScreen/></StudioShell> });
