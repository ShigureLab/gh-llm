from __future__ import annotations

import math
from typing import TYPE_CHECKING

from gh_llm.commands.options import current_timestamp_utc, parse_timeline_window
from gh_llm.models import PageInfo, TimelineContext, TimelinePage, TimelineWindow

if TYPE_CHECKING:
    from gh_llm.github_api import GitHubClient
    from gh_llm.models import PullRequestMeta, TimelineEvent

DEFAULT_PAGE_SIZE = 8


def build_context_from_meta(
    meta: PullRequestMeta,
    page_size: int,
    *,
    total_count: int | None = None,
    total_pages: int | None = None,
    fetched_at: str | None = None,
    timeline_after: str | None = None,
    timeline_before: str | None = None,
    timeline_unfiltered_count: int | None = None,
    timeline_filtered: bool = False,
    filtered_pages: dict[int, TimelinePage] | None = None,
) -> TimelineContext:
    _validate_page_size(page_size)

    timeline_loaded = total_count is not None and total_pages is not None
    resolved_total_count = 0 if total_count is None else total_count
    resolved_total_pages = 0 if total_pages is None else total_pages

    return TimelineContext(
        owner=meta.ref.owner,
        name=meta.ref.name,
        number=meta.ref.number,
        page_size=page_size,
        total_count=resolved_total_count,
        total_pages=resolved_total_pages,
        title=meta.title,
        url=meta.url,
        author=meta.author,
        state=meta.state,
        is_draft=meta.is_draft,
        body=meta.body,
        updated_at=meta.updated_at,
        fetched_at=(current_timestamp_utc() if fetched_at is None else fetched_at),
        timeline_loaded=timeline_loaded,
        timeline_after=timeline_after,
        timeline_before=timeline_before,
        timeline_unfiltered_count=timeline_unfiltered_count,
        timeline_filtered=timeline_filtered,
        labels=meta.labels,
        kind=meta.kind,
        pr_reactions_summary=meta.reactions_summary,
        can_edit_pr_body=meta.can_edit_body,
        is_merged=meta.is_merged,
        head_ref_name=meta.head_ref_name,
        head_ref_repo=meta.head_ref_repo,
        head_ref_oid=meta.head_ref_oid,
        head_ref_deleted=meta.head_ref_deleted,
        pr_node_id=meta.node_id,
        merge_state_status=meta.merge_state_status,
        mergeable=meta.mergeable,
        review_decision=meta.review_decision,
        requires_approving_reviews=meta.requires_approving_reviews,
        required_approving_review_count=meta.required_approving_review_count,
        requires_code_owner_reviews=meta.requires_code_owner_reviews,
        approved_review_count=meta.approved_review_count,
        requires_status_checks=meta.requires_status_checks,
        base_ref_name=meta.base_ref_name,
        base_ref_oid=meta.base_ref_oid,
        merge_commit_allowed=meta.merge_commit_allowed,
        squash_merge_allowed=meta.squash_merge_allowed,
        rebase_merge_allowed=meta.rebase_merge_allowed,
        co_author_trailers=meta.co_author_trailers,
        conflict_files=meta.conflict_files,
        forward_after_by_page=({1: None} if timeline_loaded and not timeline_filtered else {}),
        backward_before_by_page=({resolved_total_pages: None} if timeline_loaded and not timeline_filtered else {}),
        filtered_pages=({} if filtered_pages is None else filtered_pages),
    )


class TimelinePager:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def build_initial(
        self,
        meta: PullRequestMeta,
        page_size: int,
        *,
        timeline_window: TimelineWindow | None = None,
        show_resolved_details: bool = False,
        show_outdated_details: bool = False,
        show_minimized_details: bool = False,
        show_details_blocks: bool = False,
        review_threads_window: int | None = 10,
        diff_hunk_lines: int | None = None,
    ) -> tuple[TimelineContext, TimelinePage, TimelinePage | None]:
        _validate_page_size(page_size)
        window = TimelineWindow() if timeline_window is None else timeline_window
        fetched_at = current_timestamp_utc()

        if window.active:
            return self._build_filtered_initial(
                meta,
                page_size,
                timeline_window=window,
                fetched_at=fetched_at,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )

        first_page = self._client.fetch_timeline_forward(
            meta.ref,
            page_size=page_size,
            after=None,
            timeline_window=None,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            kind=meta.kind,
        )
        total_count = first_page.total_count
        total_pages = _page_count(total_count, page_size)
        first_page = _with_absolute_indexes(
            first_page,
            page=1,
            total_count=total_count,
            total_pages=total_pages,
            default_page_size=page_size,
        )

        context = build_context_from_meta(
            meta=meta,
            page_size=page_size,
            total_count=total_count,
            total_pages=total_pages,
            fetched_at=fetched_at,
        )
        self._remember_forward(context, page=1, cursor_used=None, page_result=first_page)

        if total_pages == 1:
            return context, first_page, None

        last_page_size = _page_size_for_page(
            page=total_pages, total_count=total_count, total_pages=total_pages, default_size=page_size
        )
        last_page = self._client.fetch_timeline_backward(
            meta.ref,
            page_size=last_page_size,
            before=None,
            timeline_window=None,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            kind=meta.kind,
        )
        last_page = _with_absolute_indexes(
            last_page,
            page=total_pages,
            total_count=total_count,
            total_pages=total_pages,
            default_page_size=page_size,
        )
        self._remember_backward(context, page=total_pages, cursor_used=None, page_result=last_page)
        return context, first_page, last_page

    def fetch_page(
        self,
        meta: PullRequestMeta,
        context: TimelineContext,
        page: int,
        *,
        show_resolved_details: bool = False,
        show_outdated_details: bool = False,
        show_minimized_details: bool = False,
        show_details_blocks: bool = False,
        review_threads_window: int | None = 10,
        diff_hunk_lines: int | None = None,
    ) -> TimelinePage:
        _validate_page(page, context.total_pages)
        if context.timeline_filtered:
            return self._fetch_filtered_page(
                meta,
                context,
                page,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )

        from_start = page - 1
        from_end = context.total_pages - page
        if from_start <= from_end:
            return self._walk_forward(
                meta,
                context,
                page,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )
        return self._walk_backward(
            meta,
            context,
            page,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
        )

    def _fetch_filtered_page(
        self,
        meta: PullRequestMeta,
        context: TimelineContext,
        page: int,
        *,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> TimelinePage:
        timeline_window = parse_timeline_window(after=context.timeline_after, before=context.timeline_before)
        if not timeline_window.active:
            result = context.filtered_pages.get(page)
            if result is None:
                raise RuntimeError(f"filtered timeline page {page} is unavailable")
            return result

        if timeline_window.after is not None:
            collected, _ = self._collect_filtered_from_end(
                meta,
                page_size=context.page_size,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )
        else:
            collected, _ = self._collect_filtered_from_start(
                meta,
                page_size=context.page_size,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )

        filtered_pages = _build_local_filtered_pages(collected=collected, page_size=context.page_size)
        result = filtered_pages.get(page)
        if result is None:
            raise RuntimeError(f"filtered timeline page {page} is unavailable")
        return result

    def _build_filtered_initial(
        self,
        meta: PullRequestMeta,
        page_size: int,
        *,
        timeline_window: TimelineWindow,
        fetched_at: str,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> tuple[TimelineContext, TimelinePage, TimelinePage | None]:
        if timeline_window.after is not None:
            collected, total_count = self._collect_filtered_from_end(
                meta,
                page_size=page_size,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )
        else:
            collected, total_count = self._collect_filtered_from_start(
                meta,
                page_size=page_size,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
            )

        filtered_pages = _build_local_filtered_pages(collected=collected, page_size=page_size)
        filtered_total_count = len(collected)
        filtered_total_pages = _page_count(filtered_total_count, page_size)
        context = build_context_from_meta(
            meta=meta,
            page_size=page_size,
            total_count=filtered_total_count,
            total_pages=filtered_total_pages,
            fetched_at=fetched_at,
            timeline_after=timeline_window.after_text,
            timeline_before=timeline_window.before_text,
            timeline_unfiltered_count=total_count,
            timeline_filtered=True,
            filtered_pages=filtered_pages,
        )
        first_page = filtered_pages[1]
        last_page = None if filtered_total_pages == 1 else filtered_pages[filtered_total_pages]
        return context, first_page, last_page

    def _collect_filtered_from_end(
        self,
        meta: PullRequestMeta,
        *,
        page_size: int,
        timeline_window: TimelineWindow,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> tuple[list[tuple[int, TimelineEvent]], int]:
        seed_page = self._client.fetch_timeline_backward(
            meta.ref,
            page_size=1,
            before=None,
            timeline_window=timeline_window,
            show_resolved_details=show_resolved_details,
            show_outdated_details=show_outdated_details,
            show_minimized_details=show_minimized_details,
            show_details_blocks=show_details_blocks,
            review_threads_window=review_threads_window,
            diff_hunk_lines=diff_hunk_lines,
            kind=meta.kind,
        )
        total_count = seed_page.total_count
        total_pages = _page_count(total_count, page_size)
        last_page_size = _page_size_for_page(
            page=total_pages,
            total_count=total_count,
            total_pages=total_pages,
            default_size=page_size,
        )

        page_number = total_pages
        current_page = seed_page
        if last_page_size != 1:
            current_page = self._client.fetch_timeline_backward(
                meta.ref,
                page_size=last_page_size,
                before=None,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
                kind=meta.kind,
            )
        current_page = _with_absolute_indexes(
            current_page,
            page=page_number,
            total_count=total_count,
            total_pages=total_pages,
            default_page_size=page_size,
        )

        collected: list[tuple[int, TimelineEvent]] = []
        while True:
            collected.extend(_matching_items(current_page, timeline_window))
            if not current_page.page_info.has_previous_page:
                break
            if (
                meta.kind != "pr"
                and current_page.items
                and timeline_window.after is not None
                and current_page.items[0].timestamp <= timeline_window.after
            ):
                break

            before_cursor = current_page.page_info.start_cursor
            if before_cursor is None:
                raise RuntimeError("timeline backward cursor unexpectedly missing")
            page_number -= 1
            current_page = self._client.fetch_timeline_backward(
                meta.ref,
                page_size=_page_size_for_page(
                    page=page_number,
                    total_count=total_count,
                    total_pages=total_pages,
                    default_size=page_size,
                ),
                before=before_cursor,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
                kind=meta.kind,
            )
            current_page = _with_absolute_indexes(
                current_page,
                page=page_number,
                total_count=total_count,
                total_pages=total_pages,
                default_page_size=page_size,
            )

        collected.sort(key=lambda item: item[0])
        return collected, total_count

    def _collect_filtered_from_start(
        self,
        meta: PullRequestMeta,
        *,
        page_size: int,
        timeline_window: TimelineWindow,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> tuple[list[tuple[int, TimelineEvent]], int]:
        page_number = 1
        after_cursor: str | None = None
        total_count: int | None = None
        total_pages: int | None = None
        collected: list[tuple[int, TimelineEvent]] = []

        while True:
            current_page = self._client.fetch_timeline_forward(
                meta.ref,
                page_size=page_size,
                after=after_cursor,
                timeline_window=timeline_window,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
                kind=meta.kind,
            )
            if total_count is None:
                total_count = current_page.total_count
                total_pages = _page_count(total_count, page_size)
            assert total_count is not None
            assert total_pages is not None

            current_page = _with_absolute_indexes(
                current_page,
                page=page_number,
                total_count=total_count,
                total_pages=total_pages,
                default_page_size=page_size,
            )
            collected.extend(_matching_items(current_page, timeline_window))

            if not current_page.page_info.has_next_page:
                break
            if (
                meta.kind != "pr"
                and current_page.items
                and timeline_window.before is not None
                and current_page.items[-1].timestamp >= timeline_window.before
            ):
                break

            after_cursor = current_page.page_info.end_cursor
            if after_cursor is None:
                raise RuntimeError("timeline forward cursor unexpectedly missing")
            page_number += 1

        assert total_count is not None
        return collected, total_count

    def _walk_forward(
        self,
        meta: PullRequestMeta,
        context: TimelineContext,
        target_page: int,
        *,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> TimelinePage:
        start_page = max(page for page in context.forward_after_by_page if page <= target_page)
        cursor = context.forward_after_by_page[start_page]

        page = start_page
        while True:
            result = self._client.fetch_timeline_forward(
                meta.ref,
                page_size=context.page_size,
                after=cursor,
                timeline_window=None,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
                kind=meta.kind,
            )
            result = _with_absolute_indexes(
                result,
                page=page,
                total_count=context.total_count,
                total_pages=context.total_pages,
                default_page_size=context.page_size,
            )
            self._remember_forward(context, page=page, cursor_used=cursor, page_result=result)
            if page == target_page:
                return result
            cursor = result.page_info.end_cursor
            if cursor is None:
                raise RuntimeError("timeline forward cursor unexpectedly missing")
            page += 1

    def _walk_backward(
        self,
        meta: PullRequestMeta,
        context: TimelineContext,
        target_page: int,
        *,
        show_resolved_details: bool,
        show_outdated_details: bool,
        show_minimized_details: bool,
        show_details_blocks: bool,
        review_threads_window: int | None,
        diff_hunk_lines: int | None,
    ) -> TimelinePage:
        start_page = min(page for page in context.backward_before_by_page if page >= target_page)
        cursor = context.backward_before_by_page[start_page]

        page = start_page
        while True:
            current_page_size = _page_size_for_page(
                page=page,
                total_count=context.total_count,
                total_pages=context.total_pages,
                default_size=context.page_size,
            )
            result = self._client.fetch_timeline_backward(
                meta.ref,
                page_size=current_page_size,
                before=cursor,
                timeline_window=None,
                show_resolved_details=show_resolved_details,
                show_outdated_details=show_outdated_details,
                show_minimized_details=show_minimized_details,
                show_details_blocks=show_details_blocks,
                review_threads_window=review_threads_window,
                diff_hunk_lines=diff_hunk_lines,
                kind=meta.kind,
            )
            result = _with_absolute_indexes(
                result,
                page=page,
                total_count=context.total_count,
                total_pages=context.total_pages,
                default_page_size=context.page_size,
            )
            self._remember_backward(context, page=page, cursor_used=cursor, page_result=result)
            if page == target_page:
                return result
            cursor = result.page_info.start_cursor
            if cursor is None:
                raise RuntimeError("timeline backward cursor unexpectedly missing")
            page -= 1

    def _remember_forward(
        self, context: TimelineContext, page: int, cursor_used: str | None, page_result: TimelinePage
    ) -> None:
        context.forward_after_by_page[page] = cursor_used
        if page_result.page_info.has_next_page and page_result.page_info.end_cursor:
            context.forward_after_by_page[page + 1] = page_result.page_info.end_cursor

    def _remember_backward(
        self, context: TimelineContext, page: int, cursor_used: str | None, page_result: TimelinePage
    ) -> None:
        context.backward_before_by_page[page] = cursor_used
        if page_result.page_info.has_previous_page and page_result.page_info.start_cursor:
            context.backward_before_by_page[page - 1] = page_result.page_info.start_cursor


def _page_count(total: int, page_size: int) -> int:
    if total == 0:
        return 1
    return math.ceil(total / page_size)


def _validate_page(page: int, total_pages: int) -> None:
    if page < 1 or page > total_pages:
        raise ValueError(f"invalid page {page}, expected in 1..{total_pages}")


def _validate_page_size(page_size: int) -> None:
    if page_size <= 0:
        raise ValueError("page-size must be greater than 0")


def _page_size_for_page(page: int, total_count: int, total_pages: int, default_size: int) -> int:
    if page == total_pages:
        remainder = total_count % default_size
        if remainder != 0:
            return remainder
    return default_size


def _matching_items(page: TimelinePage, timeline_window: TimelineWindow) -> list[tuple[int, TimelineEvent]]:
    return [
        (absolute_index, event)
        for absolute_index, event in zip(page.absolute_indexes, page.items, strict=False)
        if _event_matches_window(event, timeline_window)
    ]


def _event_matches_window(event: TimelineEvent, timeline_window: TimelineWindow) -> bool:
    if _matches_window(event.timestamp, timeline_window):
        return True
    if timeline_window.after is None:
        return False
    if event.timestamp > timeline_window.after:
        return False
    return any(_matches_window(timestamp, timeline_window) for timestamp in event.related_timestamps)


def _matches_window(timestamp: object, timeline_window: TimelineWindow) -> bool:
    from datetime import datetime

    if not isinstance(timestamp, datetime):
        return False
    if timeline_window.after is not None and timestamp <= timeline_window.after:
        return False
    if timeline_window.before is not None and timestamp >= timeline_window.before:
        return False
    return True


def _build_local_filtered_pages(
    *, collected: list[tuple[int, TimelineEvent]], page_size: int
) -> dict[int, TimelinePage]:
    total_count = len(collected)
    total_pages = _page_count(total_count, page_size)
    pages: dict[int, TimelinePage] = {}
    if not collected:
        pages[1] = TimelinePage(
            items=[],
            total_count=0,
            page_info=PageInfo(
                has_next_page=False,
                has_previous_page=False,
                start_cursor=None,
                end_cursor=None,
            ),
            absolute_indexes=(),
        )
        return pages

    for page in range(1, total_pages + 1):
        start = (page - 1) * page_size
        end = min(start + page_size, total_count)
        chunk = collected[start:end]
        pages[page] = TimelinePage(
            items=[event for _, event in chunk],
            total_count=total_count,
            page_info=PageInfo(
                has_next_page=page < total_pages,
                has_previous_page=page > 1,
                start_cursor=None,
                end_cursor=None,
            ),
            absolute_indexes=tuple(index for index, _ in chunk),
        )
    return pages


def _with_absolute_indexes(
    page_result: TimelinePage,
    *,
    page: int,
    total_count: int,
    total_pages: int,
    default_page_size: int,
) -> TimelinePage:
    start_index = _page_start_index(
        page=page,
        total_count=total_count,
        total_pages=total_pages,
        page_size=default_page_size,
        item_count=len(page_result.items),
    )
    return TimelinePage(
        items=page_result.items,
        total_count=page_result.total_count,
        page_info=page_result.page_info,
        absolute_indexes=tuple(range(start_index, start_index + len(page_result.items))),
    )


def _page_start_index(*, page: int, total_count: int, total_pages: int, page_size: int, item_count: int) -> int:
    if page == total_pages and total_count % page_size != 0:
        return max(1, total_count - item_count + 1)
    return ((page - 1) * page_size) + 1
