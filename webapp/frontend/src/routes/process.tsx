import { createFileRoute } from '@tanstack/react-router';
import { StudioShell } from '@/components/studio-ui';
import { ProcessScreen } from '@/components/studio-workspaces';
export const Route = createFileRoute('/process')({ component: () => <StudioShell><ProcessScreen/></StudioShell> });
