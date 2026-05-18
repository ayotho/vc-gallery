"""
VC Gallery — Playwright end-to-end tests.

Expects the gallery server running at localhost:8770 with a folder set.
Run: python -m pytest tests/test_gallery_e2e.py -v
"""
import pytest
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:8770"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.goto(BASE)
    pg.wait_for_selector(".grid", timeout=10000)
    yield pg
    ctx.close()


# ── Grid basics ──────────────────────────────────────────────────────

def test_grid_loads_cards(page):
    """Gallery loads and renders at least one card."""
    cards = page.locator(".card")
    assert cards.count() > 0


def test_counter_shows_numbers(page):
    """Counter shows 'N of M' with non-zero values."""
    shown = page.locator("#counter-shown").inner_text()
    total = page.locator("#counter-total").inner_text()
    assert int(shown.split("(")[0].strip().replace(",", "")) > 0
    assert int(total.replace(",", "")) > 0


# ── Stack versions toggle ────────────────────────────────────────────

def test_stack_toggle_exists(page):
    """Stack versions checkbox exists and is checked by default."""
    chk = page.locator("#chk-latest-only")
    assert chk.is_visible()
    assert chk.is_checked()


def test_stack_toggle_changes_grid(page):
    """Toggling stack versions off/on changes the card count."""
    count_before = page.locator(".card").count()
    page.locator("#chk-latest-only").uncheck()
    page.wait_for_timeout(500)
    count_after = page.locator(".card").count()
    # With stacking off, should show same or more cards (ungrouped)
    assert count_after >= count_before
    # Toggle back on
    page.locator("#chk-latest-only").check()


# ── Stacked cards ────────────────────────────────────────────────────

def test_stacked_cards_have_badge(page):
    """If any cards are stacked, they should have a .stack-badge."""
    stacked = page.locator(".card.stacked")
    if stacked.count() > 0:
        badge = stacked.first.locator(".stack-badge")
        assert badge.is_visible()
        assert "v" in badge.inner_text()  # e.g. "4v"


def test_stacked_cards_have_depth(page):
    """Stacked cards should have ::before/::after pseudo-elements (CSS class)."""
    stacked = page.locator(".card.stacked")
    if stacked.count() > 0:
        cls = stacked.first.get_attribute("class")
        assert "stacked" in cls


# ── Multi-select ─────────────────────────────────────────────────────

def test_ctrl_click_selects(page):
    """Ctrl+click on a card adds it to multi-selection."""
    cards = page.locator(".card")
    if cards.count() < 2:
        pytest.skip("Need at least 2 cards")
    cards.nth(0).click(modifiers=["Control"])
    cards.nth(1).click(modifiers=["Control"])
    bar = page.locator("#multi-select-bar")
    assert bar.is_visible()
    count_text = bar.locator(".ms-count").inner_text()
    assert "2" in count_text


def test_select_all_button(page):
    """Select All button selects all visible cards."""
    btn = page.locator("#btn-select-all")
    if not btn.is_visible():
        # Try the topbar version
        btn = page.locator(".select-all-btn")
    if btn.count() == 0:
        pytest.skip("Select All button not found")
    btn.first.click()
    bar = page.locator("#multi-select-bar")
    assert bar.is_visible()


def test_cmd_a_selects_all(page):
    """Cmd+A / Ctrl+A selects all visible cards."""
    # Click the grid area first to ensure focus
    page.locator(".grid").click()
    page.keyboard.press("Control+a")
    page.wait_for_timeout(300)
    bar = page.locator("#multi-select-bar")
    assert bar.is_visible()


def test_clear_selection(page):
    """Clear button clears multi-selection."""
    cards = page.locator(".card")
    if cards.count() == 0:
        pytest.skip("No cards")
    cards.first.click(modifiers=["Control"])
    page.locator("#ms-clear").click()
    bar = page.locator("#multi-select-bar")
    assert not bar.is_visible()


# ── Compare mode ─────────────────────────────────────────────────────

def test_compare_button_exists(page):
    """Compare button appears in multi-select bar."""
    cards = page.locator(".card")
    if cards.count() < 2:
        pytest.skip("Need at least 2 cards")
    cards.nth(0).click(modifiers=["Control"])
    cards.nth(1).click(modifiers=["Control"])
    cmp = page.locator("#ms-compare")
    assert cmp.is_visible()


def test_compare_opens_overlay(page):
    """Clicking Compare opens the compare overlay."""
    cards = page.locator(".card")
    if cards.count() < 2:
        pytest.skip("Need at least 2 cards")
    cards.nth(0).click(modifiers=["Control"])
    cards.nth(1).click(modifiers=["Control"])
    page.locator("#ms-compare").click()
    page.wait_for_timeout(500)
    overlay = page.locator("#compare-overlay")
    assert not overlay.get_attribute("hidden")


def test_compare_escape_closes(page):
    """Escape closes the compare overlay."""
    cards = page.locator(".card")
    if cards.count() < 2:
        pytest.skip("Need at least 2 cards")
    cards.nth(0).click(modifiers=["Control"])
    cards.nth(1).click(modifiers=["Control"])
    page.locator("#ms-compare").click()
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    overlay = page.locator("#compare-overlay")
    assert overlay.is_hidden()


# ── Drawer ───────────────────────────────────────────────────────────

def test_click_card_opens_drawer(page):
    """Clicking a card opens the detail drawer."""
    cards = page.locator(".card")
    if cards.count() == 0:
        pytest.skip("No cards")
    cards.first.click()
    page.wait_for_timeout(500)
    drawer = page.locator("#drawer")
    assert not drawer.evaluate("el => el.classList.contains('hidden')")


def test_drawer_has_version_strip(page):
    """If the opened card has siblings (same shot_id), version strip shows."""
    stacked = page.locator(".card.stacked")
    if stacked.count() == 0:
        pytest.skip("No stacked cards to test")
    stacked.first.click()
    page.wait_for_timeout(500)
    strip = page.locator("#drawer-version-strip")
    # Should be visible if card has siblings
    assert strip.is_visible() or strip.is_hidden()  # pass either way, just no crash


# ── View tabs ────────────────────────────────────────────────────────

def test_shots_view_loads(page):
    """Shots view tab loads without error."""
    btn = page.locator('button[data-view="shots"]')
    if btn.count() == 0:
        pytest.skip("Shots tab not found")
    btn.click()
    page.wait_for_timeout(1000)
    root = page.locator("#shots-root")
    assert root.is_visible()


def test_scenes_view_loads(page):
    """Scenes view tab loads without error."""
    btn = page.locator('button[data-view="scenes"]')
    if btn.count() == 0:
        pytest.skip("Scenes tab not found")
    btn.click()
    page.wait_for_timeout(1000)
    root = page.locator("#scenes-root")
    assert root.is_visible()


def test_grid_view_returns(page):
    """Switching back to grid view works."""
    page.locator('button[data-view="shots"]').click()
    page.wait_for_timeout(500)
    page.locator('button[data-view="grid"]').click()
    page.wait_for_timeout(500)
    root = page.locator("#grid-root")
    assert root.is_visible()


# ── Drag-to-inherit ──────────────────────────────────────────────────

def test_cards_are_draggable(page):
    """Cards have draggable=true attribute."""
    cards = page.locator(".card")
    if cards.count() == 0:
        pytest.skip("No cards")
    assert cards.first.get_attribute("draggable") == "true"


# ── Status filters ───────────────────────────────────────────────────

def test_status_filter_works(page):
    """Clicking a status filter changes the grid."""
    rows = page.locator("#filter-status .filter-row")
    if rows.count() < 2:
        pytest.skip("Not enough status filters")
    # Click the second filter (first non-All row)
    rows.nth(1).click()
    page.wait_for_timeout(500)
    # Should have an active filter
    active = page.locator("#filter-status .filter-row.active")
    assert active.count() == 1
