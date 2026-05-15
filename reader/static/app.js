/**
 * app.js - Main state machine and UI logic
 *
 * CHANGES:
 *  - Reader: Replaced single-chapter view with infinite scroll. Chapters are
 *    rendered as stacked <section> elements inside #reading-column. A window
 *    of current ±2 chapters is kept mounted; chapters outside that range are
 *    unloaded (content cleared, placeholder height preserved) so memory stays
 *    bounded regardless of library size.
 *  - Reader: IntersectionObserver watches per-chapter sentinel elements to
 *    detect which chapter is currently in view. Progress saving, title bar,
 *    and bookmark/note state all update automatically as you scroll.
 *  - Reader: Top/bottom bars are now static (in page flow) so they no longer
 *    float over content. Prev/Next buttons in the top bar jump to the
 *    adjacent chapter by scrolling it into view (loading it if needed).
 *  - Reader: navigateTo(VIEWS.READER) now accepts a chapter id and builds the
 *    initial window around that chapter.
 */

const VIEWS = {
    LIBRARY: 'library-view',
    NOVEL: 'novel-view',
    READER: 'reader-view'
};

const DEFAULT_SETTINGS = {
    theme: 'light',
    customBg: '',
    customText: '',
    fontFamily: 'Georgia, serif',
    fontSize: 18,
    lineHeight: 1.6,
    paragraphSpacing: 1.0,
    columnWidth: '70ch'
};

// How many chapters to keep loaded on each side of the current one
const CHAPTER_WINDOW = 2;

let currentState = {
    view: VIEWS.LIBRARY,
    novel: null,
    chapter: null,       // The chapter currently in the viewport
    settings: { ...DEFAULT_SETTINGS },
    filter: {
        includeTags: [],
        excludeTags: [],
        sortBy: 'title'
    }
};

// Infinite scroll state
const infiniteScroll = {
    chapterIds: [],          // Full ordered list of chapter ids for current novel
    loadedIds: new Set(),    // Chapter ids currently mounted in DOM
    observer: null,          // IntersectionObserver for sentinels
    scrollSaveTimeout: null,
    lastScrollY: 0
};

// --- API Module ---
const api = {
    async fetch(url, options = {}) {
        const resp = await fetch(url, options);
        if (!resp.ok) throw new Error(`API Error: ${resp.status}`);
        return resp.json();
    },
    getNovels: (params = {}) => {
        const query = new URLSearchParams();
        if (params.include_tags) params.include_tags.forEach(t => query.append('include_tags', t));
        if (params.exclude_tags) params.exclude_tags.forEach(t => query.append('exclude_tags', t));
        if (params.sort_by) query.append('sort_by', params.sort_by);
        return api.fetch(`/api/novels?${query.toString()}`);
    },
    getTags: () => api.fetch('/api/tags'),
    getNovel: (id) => api.fetch(`/api/novels/${id}`),
    getChapter: (id) => api.fetch(`/api/chapters/${id}`),
    search: (q) => api.fetch(`/api/search?q=${encodeURIComponent(q)}`),
    getProgress: () => api.fetch('/api/progress'),
    updateProgress: (data) => api.fetch('/api/progress', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    }),
    getBookmarks: () => api.fetch('/api/bookmarks'),
    createBookmark: (data) => api.fetch('/api/bookmarks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    }),
    deleteBookmark: (id) => api.fetch(`/api/bookmarks/${id}`, { method: 'DELETE' }),
    getNote: (chapterId) => api.fetch(`/api/notes/${chapterId}`),
    updateNote: (data) => api.fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    }),
    fetchChapters: (id) => api.fetch(`/api/novels/${id}/fetch-chapters`, { method: 'POST' }),
    updateChapters: (id) => api.fetch(`/api/novels/${id}/update-chapters`, { method: 'POST' }),
    getFetchStatus: (id) => api.fetch(`/api/novels/${id}/fetch-status`)
};

// --- Utils ---
const $ = (id) => document.getElementById(id);
const show = (el) => el.classList.remove('hidden');
const hide = (el) => el.classList.add('hidden');

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => { clearTimeout(timeout); func(...args); };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// --- Settings & Theme ---
function loadSettings() {
    const saved = localStorage.getItem('reader_settings');
    if (saved) {
        currentState.settings = { ...DEFAULT_SETTINGS, ...JSON.parse(saved) };
    }
    applySettings();
}

function saveSettings() {
    localStorage.setItem('reader_settings', JSON.stringify(currentState.settings));
    applySettings();
}

function applySettings() {
    const s = currentState.settings;
    const root = document.documentElement;

    root.setAttribute('data-theme', s.theme);

    if (s.customBg) root.style.setProperty('--bg-primary', s.customBg);
    else root.style.removeProperty('--bg-primary');

    if (s.customText) root.style.setProperty('--text-primary', s.customText);
    else root.style.removeProperty('--text-primary');

    root.style.setProperty('--font-family', s.fontFamily);
    root.style.setProperty('--font-size', `${s.fontSize}px`);
    root.style.setProperty('--line-height', s.lineHeight);
    root.style.setProperty('--paragraph-spacing', `${s.paragraphSpacing}em`);
    root.style.setProperty('--content-width', s.columnWidth);

    // Update UI controls (guards for when reader panel isn't in DOM yet)
    if ($('font-size-label')) $('font-size-label').textContent = `${s.fontSize}px`;
    if ($('line-height-label')) $('line-height-label').textContent = s.lineHeight;
    if ($('para-spacing-label')) $('para-spacing-label').textContent = `${s.paragraphSpacing}em`;
    if ($('column-width-label')) $('column-width-label').textContent = s.columnWidth;
    if ($('font-size-range')) $('font-size-range').value = s.fontSize;
    if ($('line-height-range')) $('line-height-range').value = s.lineHeight;
    if ($('para-spacing-range')) $('para-spacing-range').value = s.paragraphSpacing;
    if ($('font-family-select')) $('font-family-select').value = s.fontFamily;
    if ($('column-width-range')) $('column-width-range').value = parseInt(s.columnWidth);
    if ($('bg-color-picker')) $('bg-color-picker').value = getComputedStyle(root).getPropertyValue('--bg-primary').trim();
    if ($('text-color-picker')) $('text-color-picker').value = getComputedStyle(root).getPropertyValue('--text-primary').trim();
}

// --- Navigation ---
async function navigateTo(view, params = {}) {
    Object.values(VIEWS).forEach(v => hide($(v)));
    show($(view));
    currentState.view = view;

    if (view === VIEWS.LIBRARY) {
        await renderLibrary();
        window.scrollTo(0, 0);
    } else if (view === VIEWS.NOVEL) {
        await renderNovel(params.id);
        window.scrollTo(0, 0);
    } else if (view === VIEWS.READER) {
        await initReader(params.id);
        // Don't scrollTo(0,0) here — initReader handles scroll restoration
    }
}

// --- Library View ---
let allTags = [];

async function renderLibrary() {
    if (allTags.length === 0) {
        allTags = await api.getTags();
        renderTagFilters();
    }

    const novels = await api.getNovels({
        include_tags: currentState.filter.includeTags,
        exclude_tags: currentState.filter.excludeTags,
        sort_by: currentState.filter.sortBy
    });

    const grid = $('novel-grid');
    grid.innerHTML = '';

    novels.forEach(n => {
        const card = document.createElement('div');
        card.className = 'novel-card';
        const progress = n.chapter_count > 0 ? (n.chapters_read / n.chapter_count) * 100 : 0;
        const initials = n.title.split(' ').map(w => w[0]).join('').substring(0, 3).toUpperCase();

        card.innerHTML = `
            ${n.cover_path ? `<img src="/api/covers/${n.id}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'">` : ''}
            <div class="placeholder-cover" style="${n.cover_path ? 'display:none' : 'display:flex'}">${initials}</div>
            <div class="novel-card-info">
                <h3>${n.title}</h3>
                <p>${n.author || 'Unknown Author'}</p>
                <div class="progress-bar-container">
                    <div class="progress-bar-fill" style="width: ${progress}%"></div>
                </div>
                <p style="font-size: 0.75rem; margin-top: 5px">${n.chapters_read} / ${n.chapter_count} read</p>
            </div>
        `;
        card.onclick = () => navigateTo(VIEWS.NOVEL, { id: n.id });
        grid.appendChild(card);
    });
}

function renderTagFilters() {
    const section = $('tag-filter-section');
    section.innerHTML = '';

    const header = document.createElement('div');
    header.className = 'tag-panel-header';
    header.style.cssText = 'cursor:pointer;margin-bottom:8px;font-weight:bold;display:flex;align-items:center;';

    const title = document.createElement('h4');
    title.id = 'tag-panel-title';
    title.style.margin = '0';
    header.appendChild(title);
    section.appendChild(header);

    const container = document.createElement('div');
    container.id = 'tag-filter-container';
    container.className = 'tag-filter-container';
    section.appendChild(container);

    const setTagPanelState = (open) => {
        container.style.display = open ? 'flex' : 'none';
        title.textContent = open ? 'Filter by Tags ▼' : 'Filter by Tags ▶';
        localStorage.setItem('tagPanelOpen', String(open));
    };

    header.onclick = () => setTagPanelState(container.style.display === 'none');

    const hasActiveFilters = currentState.filter.includeTags.length > 0 || currentState.filter.excludeTags.length > 0;
    setTagPanelState(hasActiveFilters || localStorage.getItem('tagPanelOpen') === 'true');

    allTags.forEach(tagObj => {
        const tag = tagObj.name;
        const count = tagObj.count;
        const el = document.createElement('span');
        el.className = 'filter-tag';
        if (currentState.filter.includeTags.includes(tag)) el.classList.add('include');
        if (currentState.filter.excludeTags.includes(tag)) el.classList.add('exclude');
        el.textContent = `${tag} (${count})`;
        el.onclick = (e) => {
            e.stopPropagation();
            if (el.classList.contains('include')) {
                el.classList.replace('include', 'exclude');
                currentState.filter.includeTags = currentState.filter.includeTags.filter(t => t !== tag);
                currentState.filter.excludeTags.push(tag);
            } else if (el.classList.contains('exclude')) {
                el.classList.remove('exclude');
                currentState.filter.excludeTags = currentState.filter.excludeTags.filter(t => t !== tag);
            } else {
                el.classList.add('include');
                currentState.filter.includeTags.push(tag);
            }
            renderLibrary();
        };
        container.appendChild(el);
    });
}

// --- Novel View ---
async function renderNovel(id) {
    const novel = await api.getNovel(id);
    currentState.novel = novel;

    const details = $('novel-details');
    const initials = (novel.title || '').split(' ').map(w => w[0]).join('').substring(0, 3).toUpperCase();
    const tagsHtml = (novel.tags || []).map(t => {
        const name = typeof t === 'object' ? t.name : t;
        return `<span class="tag">${name}</span>`;
    }).join('');

    details.innerHTML = `
        <div class="novel-cover-wrapper">
            ${novel.cover_path ? `<img src="/api/covers/${novel.id}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'">` : ''}
            <div class="placeholder-cover" style="${novel.cover_path ? 'display:none' : 'display:flex'}; width:250px">${initials}</div>
        </div>
        <div class="novel-info-text">
            <h2>${novel.title}</h2>
            <p><strong>Author:</strong> ${novel.author || 'Unknown'}</p>
            <p><strong>Status:</strong> ${novel.status || 'Unknown'}</p>
            <p><strong>Content:</strong> ${novel.content_status || 'metadata'}</p>
            <div class="novel-actions" style="margin: 15px 0">
                <button id="continue-reading-btn" class="primary-btn">Continue Reading</button>
            </div>
            <div class="tags">${tagsHtml}</div>
            <div class="synopsis">${novel.synopsis || 'No synopsis available.'}</div>
            <div id="chapter-actions" class="chapter-actions" style="margin: 20px 0; padding: 15px; background: var(--bg-secondary); border-radius: 8px;"></div>
        </div>
    `;

    renderChapterActionButton(novel);

    const list = $('chapter-list');
    list.innerHTML = '';
    (novel.chapters || []).forEach(ch => {
        const li = document.createElement('li');
        li.className = 'chapter-item';
        li.innerHTML = `<span class="read-dot ${ch.is_read ? 'visible' : ''}"></span><span>${ch.chapter_title}</span>`;
        li.onclick = () => navigateTo(VIEWS.READER, { id: ch.id });
        list.appendChild(li);
    });

    $('continue-reading-btn').onclick = async () => {
        const allProgress = await api.getProgress();
        const novelProgress = allProgress
            .filter(p => p.novel_id === novel.id)
            .sort((a, b) => new Date(b.read_at) - new Date(a.read_at));

        if (novelProgress.length > 0) {
            navigateTo(VIEWS.READER, { id: novelProgress[0].chapter_id });
        } else if (novel.chapters && novel.chapters.length > 0) {
            navigateTo(VIEWS.READER, { id: novel.chapters[0].id });
        }
    };
}

let fetchStatusInterval = null;

function renderChapterActionButton(novel) {
    const container = $('chapter-actions');
    if (!container) return;
    if (fetchStatusInterval) { clearInterval(fetchStatusInterval); fetchStatusInterval = null; }

    const status = novel.content_status || 'metadata';
    if (status === 'discovered' || status === 'metadata') {
        container.innerHTML = `<button id="fetch-chapters-btn" class="primary-btn">Download Chapters</button>`;
        $('fetch-chapters-btn').onclick = async () => {
            await api.fetchChapters(novel.id);
            startPollingFetchStatus(novel.id);
        };
    } else if (status === 'full') {
        container.innerHTML = `<button id="update-chapters-btn" class="primary-btn">Update Chapters</button>`;
        $('update-chapters-btn').onclick = async () => {
            await api.updateChapters(novel.id);
            startPollingFetchStatus(novel.id);
        };
    }
}

function startPollingFetchStatus(novelId) {
    const container = $('chapter-actions');
    container.innerHTML = `
        <div class="fetch-progress">
            <p id="fetch-status-text">Refreshing metadata...</p>
            <div class="progress-bar-container" style="margin-top: 10px">
                <div id="fetch-progress-fill" class="progress-bar-fill" style="width: 0%"></div>
            </div>
        </div>
    `;
    const startTime = Date.now();
    fetchStatusInterval = setInterval(async () => {
        try {
            const data = await api.getFetchStatus(novelId);
            const text = $('fetch-status-text');
            const fill = $('fetch-progress-fill');
            if (text && fill) {
                const elapsed = (Date.now() - startTime) / 1000;
                const progress = data.total_chapters > 0 ? (data.downloaded_chapters / data.total_chapters) * 100 : 0;
                text.textContent = (data.downloaded_chapters > 0 || elapsed > 10)
                    ? `Downloading... ${data.downloaded_chapters} / ${data.total_chapters} chapters`
                    : 'Refreshing metadata...';
                fill.style.width = `${progress}%`;
                if (data.downloaded_chapters >= data.total_chapters && data.content_status === 'full') {
                    clearInterval(fetchStatusInterval);
                    fetchStatusInterval = null;
                    text.textContent = '✓ Download complete';
                    setTimeout(() => navigateTo(VIEWS.NOVEL, { id: novelId }), 3000);
                }
            }
        } catch (e) {
            console.error("Polling error:", e);
            clearInterval(fetchStatusInterval);
        }
    }, 3000);
}

// ─── Infinite Scroll Reader ───────────────────────────────────────────────────

/**
 * Builds the DOM id for a chapter's section element.
 * @param {number} chapterId
 * @returns {string}
 */
function chapterSectionId(chapterId) {
    return `ch-section-${chapterId}`;
}

/**
 * Creates a placeholder <section> for a chapter that isn't loaded yet.
 * The element holds the chapter's position in the DOM without content.
 *
 * @param {number} chapterId
 * @returns {HTMLElement}
 */
function createChapterPlaceholder(chapterId) {
    const section = document.createElement('section');
    section.className = 'chapter-section';
    section.id = chapterSectionId(chapterId);
    section.dataset.chapterId = String(chapterId);
    section.dataset.loaded = 'false';
    section.style.minHeight = '200px'; // Prevents layout jumps when loading
    return section;
}

/**
 * Renders chapter content into its section element.
 * Fetches from API if not already loaded.
 *
 * @param {number} chapterId
 * @returns {Promise<object|null>} The chapter data, or null on failure.
 */
async function loadChapterSection(chapterId) {
    const sectionId = chapterSectionId(chapterId);
    let section = document.getElementById(sectionId);

    if (!section) {
        section = createChapterPlaceholder(chapterId);
        $('reading-column').appendChild(section);
    }

    if (section.dataset.loaded === 'true') return null; // Already loaded

    let chapter;
    try {
        chapter = await api.getChapter(chapterId);
    } catch (e) {
        section.innerHTML = `<p style="color:red;text-align:center">Failed to load chapter.</p>`;
        return null;
    }

    let content = chapter.content || '';
    if (chapter.content_type === 'plain') {
        content = content.split(/\n\n+/).map(p => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
    }

    const readingTime = Math.ceil((chapter.word_count || 0) / 250);

    section.dataset.loaded = 'true';
    section.dataset.novelId = String(chapter.novel_id);
    section.innerHTML = `
        <div class="chapter-sentinel" data-chapter-id="${chapterId}"></div>
        <h2 class="chapter-section-title">${chapter.chapter_title}</h2>
        <p style="text-align:center;font-style:italic;color:var(--text-secondary);margin-bottom:2em">
            ${chapter.word_count} words • ~${readingTime} min read
        </p>
        <div class="chapter-body">${content}</div>
    `;
    section.style.minHeight = '';

    // Re-observe the new sentinel
    const sentinel = section.querySelector('.chapter-sentinel');
    if (infiniteScroll.observer && sentinel) {
        infiniteScroll.observer.observe(sentinel);
    }

    infiniteScroll.loadedIds.add(chapterId);
    return chapter;
}

/**
 * Unloads a chapter section — clears its content but preserves its place in
 * the DOM with a fixed height so scroll position doesn't jump.
 *
 * @param {number} chapterId
 */
function unloadChapterSection(chapterId) {
    const section = document.getElementById(chapterSectionId(chapterId));
    if (!section || section.dataset.loaded !== 'true') return;

    const height = section.offsetHeight;
    section.innerHTML = '';
    section.style.minHeight = `${height}px`;
    section.dataset.loaded = 'false';
    infiniteScroll.loadedIds.delete(chapterId);
}

/**
 * Ensures the chapter window (current ±CHAPTER_WINDOW) is loaded and chapters
 * outside that range are unloaded.
 *
 * @param {number} currentChapterId  The chapter currently in the viewport.
 */
async function updateChapterWindow(currentChapterId) {
    const ids = infiniteScroll.chapterIds;
    const currentIndex = ids.indexOf(currentChapterId);
    if (currentIndex === -1) return;

    const windowStart = Math.max(0, currentIndex - CHAPTER_WINDOW);
    const windowEnd = Math.min(ids.length - 1, currentIndex + CHAPTER_WINDOW);
    const windowSet = new Set(ids.slice(windowStart, windowEnd + 1));

    // Ensure placeholder sections exist for the full window first
    for (let i = windowStart; i <= windowEnd; i++) {
        const id = ids[i];
        if (!document.getElementById(chapterSectionId(id))) {
            // Insert in correct order
            const section = createChapterPlaceholder(id);
            const col = $('reading-column');
            // Find the right insertion point
            const existingSections = [...col.querySelectorAll('.chapter-section')];
            const nextSection = existingSections.find(s => ids.indexOf(Number(s.dataset.chapterId)) > i);
            if (nextSection) col.insertBefore(section, nextSection);
            else col.appendChild(section);
        }
    }

    // Load chapters in window
    const loadPromises = [];
    for (let i = windowStart; i <= windowEnd; i++) {
        loadPromises.push(loadChapterSection(ids[i]));
    }
    await Promise.all(loadPromises);

    // Unload chapters outside window
    for (const loadedId of [...infiniteScroll.loadedIds]) {
        if (!windowSet.has(loadedId)) {
            unloadChapterSection(loadedId);
        }
    }
}

/**
 * Called by IntersectionObserver when a chapter sentinel crosses into view.
 * Updates the active chapter, saves progress, and adjusts the chapter window.
 *
 * @param {number} chapterId  The chapter whose sentinel became visible.
 */
async function onChapterVisible(chapterId) {
    if (currentState.chapter && currentState.chapter.id === chapterId) return;

    // Fetch chapter metadata (lightweight — content already in DOM)
    try {
        const chapter = await api.getChapter(chapterId);
        currentState.chapter = chapter;

        // Update top bar title
        $('reader-title').textContent = `${currentState.novel ? currentState.novel.title + ' > ' : ''}${chapter.chapter_title}`;

        // Update chapter index display
        if (currentState.novel) {
            const idx = currentState.novel.chapters.findIndex(c => c.id === chapterId);
            $('chapter-index-info').textContent = `Chapter ${idx + 1} of ${currentState.novel.chapters.length}`;
        }

        // Update prev/next buttons
        updateNavButtons(chapter);

        // Update bookmark and note icons
        updateBookmarkIcon();
        updateNoteIcon();

        // Save progress for entering this chapter
        api.updateProgress({
            novel_id: chapter.novel_id,
            chapter_id: chapterId,
            scroll_position: 0.01
        }).catch(() => {});

        // Expand the load window around the new active chapter
        await updateChapterWindow(chapterId);

    } catch (e) {
        console.error('onChapterVisible error:', e);
    }
}

/**
 * Updates the prev/next navigation buttons in the top bar.
 *
 * @param {object} chapter  Chapter object with prev_chapter_id and next_chapter_id.
 */
function updateNavButtons(chapter) {
    const goTo = (targetId) => {
        if (!targetId) return;
        const section = document.getElementById(chapterSectionId(targetId));
        if (section) {
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
            // Section not in DOM yet — load it then scroll
            loadChapterSection(targetId).then(() => {
                document.getElementById(chapterSectionId(targetId))
                    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
        }
    };

    const prevId = chapter.prev_chapter_id;
    const nextId = chapter.next_chapter_id;

    $('prev-ch-btn-top').onclick = () => goTo(prevId);
    $('next-ch-btn-top').onclick = () => goTo(nextId);
    $('prev-ch-btn-top').disabled = !prevId;
    $('next-ch-btn-top').disabled = !nextId;
    $('prev-ch-btn-top').style.opacity = prevId ? 1 : 0.3;
    $('next-ch-btn-top').style.opacity = nextId ? 1 : 0.3;
}

/**
 * Initialises the infinite scroll reader for a given starting chapter.
 * Tears down any previous reader state, builds the chapter ID list,
 * loads the initial window, and restores scroll position.
 *
 * @param {number} startChapterId  The chapter to open initially.
 */
async function initReader(startChapterId) {
    // --- Tear down previous reader ---
    if (infiniteScroll.observer) {
        infiniteScroll.observer.disconnect();
        infiniteScroll.observer = null;
    }
    infiniteScroll.loadedIds.clear();
    infiniteScroll.chapterIds = [];

    const col = $('reading-column');
    col.innerHTML = '';

    // --- Ensure we have a novel loaded ---
    if (!currentState.novel) {
        try {
            const chapter = await api.getChapter(startChapterId);
            currentState.novel = await api.getNovel(chapter.novel_id);
        } catch (e) {
            console.error('initReader: could not load novel context', e);
            return;
        }
    }

    // Build the full ordered chapter id list
    infiniteScroll.chapterIds = (currentState.novel.chapters || []).map(c => c.id);

    // --- Set up IntersectionObserver ---
    // Fires when a chapter's sentinel (top of the chapter body) enters view
    infiniteScroll.observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (entry.isIntersecting) {
                const chId = Number(entry.target.dataset.chapterId);
                onChapterVisible(chId);
                break; // Only handle the first visible one per callback batch
            }
        }
    }, {
        rootMargin: '0px 0px -70% 0px', // Trigger when sentinel is near top of viewport
        threshold: 0
    });

    // --- Load initial window ---
    await updateChapterWindow(startChapterId);

    // --- Restore saved scroll position ---
    try {
        const allProgress = await api.getProgress();
        const prog = allProgress.find(p => p.chapter_id === startChapterId);
        const section = document.getElementById(chapterSectionId(startChapterId));
        if (prog && prog.scroll_position > 0.01 && prog.scroll_position < 0.99 && section) {
            setTimeout(() => {
                const sectionTop = section.getBoundingClientRect().top + window.scrollY;
                const sectionHeight = section.offsetHeight;
                window.scrollTo(0, sectionTop + sectionHeight * prog.scroll_position);
            }, 150);
        } else if (section) {
            setTimeout(() => section.scrollIntoView({ block: 'start' }), 50);
        }
    } catch (e) {
        console.error('initReader: progress restore failed', e);
    }

    // Set initial chapter state
    try {
        const chapter = await api.getChapter(startChapterId);
        currentState.chapter = chapter;
        $('reader-title').textContent = `${currentState.novel.title} > ${chapter.chapter_title}`;
        if (currentState.novel) {
            const idx = currentState.novel.chapters.findIndex(c => c.id === startChapterId);
            $('chapter-index-info').textContent = `Chapter ${idx + 1} of ${currentState.novel.chapters.length}`;
        }
        updateNavButtons(chapter);
        updateBookmarkIcon();
        updateNoteIcon();
    } catch (e) {
        console.error('initReader: initial chapter state failed', e);
    }
}

// --- Scroll handler — saves progress for the current active chapter ---
function handleScroll() {
    if (currentState.view !== VIEWS.READER || !currentState.chapter) return;

    const scrollY = window.scrollY;
    if (Math.abs(scrollY - infiniteScroll.lastScrollY) < 50) return;
    infiniteScroll.lastScrollY = scrollY;

    clearTimeout(infiniteScroll.scrollSaveTimeout);
    infiniteScroll.scrollSaveTimeout = setTimeout(() => {
        // Calculate scroll position relative to the current chapter's section
        const section = document.getElementById(chapterSectionId(currentState.chapter.id));
        if (!section) return;

        const sectionTop = section.getBoundingClientRect().top + scrollY;
        const sectionHeight = section.offsetHeight || 1;
        const scrollPos = Math.min(1.0, Math.max(0, (scrollY - sectionTop) / sectionHeight));
        const savePos = scrollPos >= 0.9 ? 1.0 : scrollPos;

        api.updateProgress({
            novel_id: currentState.chapter.novel_id,
            chapter_id: currentState.chapter.id,
            scroll_position: savePos
        }).catch(() => {});

        // Update progress bar based on position within entire page
        const pageScrollPos = scrollY / (document.body.scrollHeight - window.innerHeight || 1);
        const bar = $('reader-progress-bar');
        if (bar) bar.style.width = `${pageScrollPos * 100}%`;

    }, 1500);
}

// --- Feature Logic ---

async function updateBookmarkIcon() {
    if (!currentState.chapter) return;
    try {
        const bookmarks = await api.getBookmarks();
        const existing = bookmarks.find(b => b.chapter_id === currentState.chapter.id);
        const btn = $('bookmark-btn');
        if (btn) btn.style.color = existing ? 'var(--accent)' : 'inherit';
    } catch (e) { /* ignore */ }
}

async function updateNoteIcon() {
    if (!currentState.chapter) return;
    try {
        const note = await api.getNote(currentState.chapter.id);
        const btn = $('notes-btn');
        if (btn) btn.textContent = note.content ? '📝✅' : '📝';
    } catch (e) { /* ignore */ }
}

// --- Event Listeners ---

window.addEventListener('scroll', handleScroll);

document.querySelectorAll('.back-btn').forEach(btn => {
    btn.onclick = () => navigateTo(VIEWS[btn.dataset.target]);
});

// Settings Panel
$('settings-btn').onclick = () => {
    $('settings-panel').classList.toggle('hidden');
    hide($('notes-panel'));
};
$('close-settings-btn').onclick = () => hide($('settings-panel'));

document.querySelectorAll('.theme-presets button').forEach(btn => {
    btn.onclick = () => {
        currentState.settings.theme = btn.dataset.theme;
        currentState.settings.customBg = '';
        currentState.settings.customText = '';
        saveSettings();
    };
});

$('bg-color-picker').oninput = (e) => { currentState.settings.customBg = e.target.value; saveSettings(); };
$('text-color-picker').oninput = (e) => { currentState.settings.customText = e.target.value; saveSettings(); };
$('font-family-select').onchange = (e) => { currentState.settings.fontFamily = e.target.value; saveSettings(); };
$('font-size-range').oninput = (e) => { currentState.settings.fontSize = parseInt(e.target.value); saveSettings(); };
$('line-height-range').oninput = (e) => { currentState.settings.lineHeight = parseFloat(e.target.value); saveSettings(); };
$('para-spacing-range').oninput = (e) => { currentState.settings.paragraphSpacing = parseFloat(e.target.value); saveSettings(); };
$('column-width-range').oninput = (e) => { currentState.settings.columnWidth = `${e.target.value}ch`; saveSettings(); };
$('reset-settings-btn').onclick = () => { currentState.settings = { ...DEFAULT_SETTINGS }; saveSettings(); };

// Sort
$('sort-select').onchange = (e) => { currentState.filter.sortBy = e.target.value; renderLibrary(); };

// Bookmarks
$('bookmark-btn').onclick = async () => {
    if (!currentState.chapter) return;
    const bookmarks = await api.getBookmarks();
    const existing = bookmarks.find(b => b.chapter_id === currentState.chapter.id);
    if (existing) {
        await api.deleteBookmark(existing.id);
    } else {
        await api.createBookmark({
            novel_id: currentState.chapter.novel_id,
            chapter_id: currentState.chapter.id,
            label: currentState.chapter.chapter_title,
            scroll_position: window.scrollY / document.body.scrollHeight
        });
    }
    updateBookmarkIcon();
};

// Notes
$('notes-btn').onclick = async () => {
    if (!currentState.chapter) return;
    const panel = $('notes-panel');
    panel.classList.toggle('hidden');
    hide($('settings-panel'));
    if (!panel.classList.contains('hidden')) {
        const note = await api.getNote(currentState.chapter.id);
        $('note-textarea').value = note.content;
    }
};

$('note-textarea').oninput = debounce(async (e) => {
    if (!currentState.chapter) return;
    await api.updateNote({ chapter_id: currentState.chapter.id, content: e.target.value });
    updateNoteIcon();
}, 500);

// Search
const toggleSearch = () => {
    const modal = $('search-modal');
    modal.classList.toggle('hidden');
    if (!modal.classList.contains('hidden')) $('global-search-input').focus();
};
$('search-toggle-btn').onclick = toggleSearch;

$('global-search-input').oninput = debounce(async (e) => {
    const q = e.target.value;
    if (q.length < 2) return;
    const results = await api.search(q);
    $('novel-results').innerHTML = results.novels.map(n =>
        `<div class="search-result-item" onclick="app.navToNovel(${n.id})">${n.title}</div>`
    ).join('');
    $('chapter-results').innerHTML = results.chapters.map(c =>
        `<div class="search-result-item" onclick="app.navToChapter(${c.id})">${c.chapter_title}</div>`
    ).join('');
}, 300);

window.app = {
    navToNovel: (id) => { hide($('search-modal')); navigateTo(VIEWS.NOVEL, { id }); },
    navToChapter: (id) => { hide($('search-modal')); navigateTo(VIEWS.READER, { id }); }
};

// Keyboard Shortcuts
window.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); toggleSearch(); }

    if (currentState.view === VIEWS.READER) {
        if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
        if (e.key === 'ArrowRight' || e.key === 'l') $('next-ch-btn-top').click();
        if (e.key === 'ArrowLeft' || e.key === 'h') $('prev-ch-btn-top').click();
        if (e.key === 'b') $('bookmark-btn').click();
        if (e.key === 'n') $('notes-btn').click();
        if (e.key === 's') $('settings-btn').click();
        if (e.key === 'f') {
            if (!document.fullscreenElement) document.documentElement.requestFullscreen();
            else document.exitFullscreen();
        }
        if (e.key === 'Escape') { hide($('settings-panel')); hide($('notes-panel')); hide($('search-modal')); }
    }
};

// Initialization
loadSettings();
navigateTo(VIEWS.LIBRARY);