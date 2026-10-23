<!-- LOVABLE:BEGIN -->
> [!IMPORTANT]
> This project is connected to [Lovable](https://lovable.dev). Avoid rewriting
> published git history — force pushing, or rebasing/amending/squashing commits
> that are already pushed — as it rewrites history on Lovable's side and the
> user will likely lose their project history.
>
> Commits you push to the connected branch sync back to Lovable and show up in
> the editor, so keep the branch in a working state.
<!-- LOVABLE:END -->

## Application architecture
- Keep shared local media and subtitle configuration in the root StudioProvider so navigation does not discard an open session.
- Use distinct TanStack content routes for Home, Process, Subtitles, Jobs, and Tools so each workspace supports direct navigation and unique metadata.
- Parse SRT with the browser-safe srt-parser-2 package instead of bespoke parsing to support standard cue formats.
- Keep unsupported processing capabilities disabled and never synthesize job progress; Python integration requires actual bot source and an authenticated service.
- Define visual roles and responsive layouts in src/styles.css; only user-selected subtitle rendering values may be applied dynamically to the preview.
