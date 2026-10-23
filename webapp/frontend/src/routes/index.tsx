import { createFileRoute } from '@tanstack/react-router';
import { HomeScreen, StudioShell } from '@/components/studio-ui';
export const Route = createFileRoute('/')({ component: () => <StudioShell><HomeScreen/></StudioShell> });
