import { createFileRoute } from '@tanstack/react-router';
import { StudioShell } from '@/components/studio-ui';
import { SubtitleScreen } from '@/components/studio-workspaces';
export const Route = createFileRoute('/subtitles')({ component: () => <StudioShell><SubtitleScreen/></StudioShell> });
