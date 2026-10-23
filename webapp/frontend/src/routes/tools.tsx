import { createFileRoute } from '@tanstack/react-router';
import { StudioShell } from '@/components/studio-ui';
import { ToolsScreen } from '@/components/studio-workspaces';
export const Route = createFileRoute('/tools')({ component: () => <StudioShell><ToolsScreen/></StudioShell> });
