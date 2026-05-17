/**
 * app.js - Main state machine and UI logic
 *
 * CHANGES in this version:
 *  - Tags moved to a left side panel with search, sort (count/alpha), and
 *    expand/collapse via a header toggle button.
 *  - Search converted from a modal to a left side panel with toggle button.
 *  - Library uses infinite scroll: loads 50 novels at a time, appends next
 *    batch when user scrolls near the bottom.
 *  - "Filter current list..." input now filters the loaded novels client-side
 *    by title/author in real time.
 *  - Browser history (pushState/popstate) support: back/forward buttons work.
 *    Each navigation (library, novel, reader) pushes a history state.
 *  - Reader: Replaced single-chapter view with infinite scroll. Chapters are
 *    rendered as stacked <section> elements inside #reading-column.
 *  - Reader: IntersectionObserver watches per-chapter sentinel elements.
 *  - Reader: Top/bottom bars are static (in page flow).
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
    columnWidth: '90ch'
};

const LIBRARY_PAGE_SIZE = 50;
const CHAPTER_WINDOW = 2;

let currentState = {
    view: VIEWS.LIBRARY,
    novel: null,
    chapter: null,
    settings: { ...DEFAULT_SETTINGS },
    filter: {
        includeTags: [],
        excludeTags: [],
        sortBy: 'title'
    }
};

// Infinite scroll state (reader)
const infiniteScroll = {
    chapterIds: [],
    loadedIds: new Set(),
    observer: null,
    scrollSaveTimeout: null,
    lastScrollY: 0
};

// Library pagination state
const libraryState = {
    allNovels: [],          // All novels loaded so far
    totalCount: 0,          // Total novels matching current filter
    loadedCount: 0,         // How many we've loaded
    isLoading: false,       // Fetch in progress
    scrollObserver: null,   // IntersectionObserver for library
    tagSearch: '',          // Current tag search filter
    tagSortBy: 'count'      // 'count' or 'name'
};

// --- API Module ---
const api = {
    async fetch(url, options = {}) {
        const resp = await fetch(url, options);
        if (!resp.ok) throw new Error(`API Error: ${resp.status}`);
        return resp.json();
    },
    getNovels: (params = {}, limit, offset) => {
        const query = new URLSearchParams();
        if (params.include_tags) params.include_tags.forEach(t => query.append('include_tags', t));
        if (params.exclude_tags) params.exclude_tags.forEach(t => query.append('exclude_tags', t));
        if (params.sort_by) query.append('sort_by', params.sort_by);
        if (limit !== undefined) query.append('limit', limit);
        if (offset !== undefined) query.append('offset', offset);
        return api.fetch(`/api/novels?${query.toString()}`);
    },
    getTags: (sortBy = 'count') => api.fetch(`/api/tags?sort_by=${sortBy}`),
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

/** Convert "rgb(r, g, b)" or "rgba(r, g, b, a)" to "#rrggbb" for <input type="color">. */
function rgbToHex(rgb) {
    const m = rgb.match(/(\d+)/g);
    if (!m) return '#000000';
    return '#' + m.slice(0, 3).map(x => parseInt(x).toString(16).padStart(2, '0')).join('');
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

    if ($('font-size-label')) $('font-size-label').textContent = `${s.fontSize}px`;
    if ($('line-height-label')) $('line-height-label').textContent = s.lineHeight;
    if ($('para-spacing-label')) $('para-spacing-label').textContent = `${s.paragraphSpacing}em`;
    if ($('column-width-label')) $('column-width-label').textContent = s.columnWidth;
    if ($('font-size-range')) $('font-size-range').value = s.fontSize;
    if ($('line-height-range')) $('line-height-range').value = s.lineHeight;
    if ($('para-spacing-range')) $('para-spacing-range').value = s.paragraphSpacing;
    if ($('font-family-select')) $('font-family-select').value = s.fontFamily;
    if ($('column-width-range')) $('column-width-range').value = parseInt(s.columnWidth);
    if ($('bg-color-picker')) {
        const bg = getComputedStyle(root).getPropertyValue('--bg-primary').trim();
        $('bg-color-picker').value = bg.startsWith('#') ? bg : rgbToHex(bg);
    }
    if ($('text-color-picker')) {
        const txt = getComputedStyle(root).getPropertyValue('--text-primary').trim();
        $('text-color-picker').value = txt.startsWith('#') ? txt : rgbToHex(txt);
    }
}

// --- Browser History ---
function pushHistory(state, title, url) {
    history.pushState(state, title, url);
}

function initPopstateHandler() {
    window.addEventListener('popstate', (e) => {
        if (!e.state) {
            // No state — go to library
            navigateTo(VIEWS.LIBRARY, {}, false);
            return;
        }
        const s = e.state;
        if (s.view === VIEWS.LIBRARY) {
            navigateTo(VIEWS.LIBRARY, {}, false);
        } else if (s.view === VIEWS.NOVEL && s.novelId) {
            navigateTo(VIEWS.NOVEL, { id: s.novelId }, false);
        } else if (s.view === VIEWS.READER && s.chapterId) {
            navigateTo(VIEWS.READER, { id: s.chapterId }, false);
        }
    });
}

// --- Navigation ---
async function navigateTo(view, params = {}, push = true) {
    Object.values(VIEWS).forEach(v => hide($(v)));
    show($(view));
    currentState.view = view;

    // Push browser history
    if (push) {
        if (view === VIEWS.LIBRARY) {
            pushHistory({ view: VIEWS.LIBRARY }, 'Library', '#library');
        } else if (view === VIEWS.NOVEL) {
            pushHistory({ view: VIEWS.NOVEL, novelId: params.id }, 'Novel', `#novel/${params.id}`);
        } else if (view === VIEWS.READER) {
            pushHistory({ view: VIEWS.READER, chapterId: params.id }, 'Reader', `#reader/${params.id}`);
        }
    }

    if (view === VIEWS.LIBRARY) {
        await renderLibrary(true);
        window.scrollTo(0, 0);
    } else if (view === VIEWS.NOVEL) {
        await renderNovel(params.id);
        window.scrollTo(0, 0);
    } else if (view === VIEWS.READER) {
        await initReader(params.id);
    }
}

// --- Library View ---

async function renderLibrary(reset = false) {
    // Reset pagination state if filters changed
    if (reset) {
        libraryState.allNovels = [];
        libraryState.loadedCount = 0;
        libraryState.isLoading = false;
        libraryState.totalCount = 0;
    }

    // Load tags on first render
    if (!libraryState.tagsLoaded) {
        await loadTags();
        libraryState.tagsLoaded = true;
    }

    // Fetch first page
    await loadMoreNovels();

    // Set up infinite scroll observer
    setupLibraryInfiniteScroll();
}

async function loadMoreNovels() {
    if (libraryState.isLoading) return;
    if (libraryState.loadedCount > 0 && libraryState.loadedCount >= libraryState.totalCount) return;

    libraryState.isLoading = true;
    const loader = $('library-loader');
    if (loader) show(loader);

    try {
        const result = await api.getNovels({
            include_tags: currentState.filter.includeTags,
            exclude_tags: currentState.filter.excludeTags,
            sort_by: currentState.filter.sortBy
        }, LIBRARY_PAGE_SIZE, libraryState.loadedCount);

        libraryState.totalCount = result.total;
        libraryState.allNovels = libraryState.allNovels.concat(result.novels);
        libraryState.loadedCount += result.novels.length;

        // Append new novels to grid
        const grid = $('novel-grid');
        result.novels.forEach(n => {
            const card = createNovelCard(n);
            grid.appendChild(card);
        });

        // Show/hide loader
        if (libraryState.loadedCount < libraryState.totalCount) {
            if (loader) show(loader);
        } else {
            if (loader) hide(loader);
        }
    } catch (e) {
        console.error('loadMoreNovels error:', e);
    } finally {
        libraryState.isLoading = false;
    }
}

function createNovelCard(n) {
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
    return card;
}

function setupLibraryInfiniteScroll() {
    if (libraryState.scrollObserver) {
        libraryState.scrollObserver.disconnect();
    }

    const sentinel = document.createElement('div');
    sentinel.id = 'library-scroll-sentinel';
    sentinel.style.height = '1px';
    $('novel-grid').parentNode.appendChild(sentinel);

    libraryState.scrollObserver = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting) {
            loadMoreNovels();
        }
    }, {
        rootMargin: '200px'
    });

    libraryState.scrollObserver.observe(sentinel);
}

// --- Tag Panel ---
async function loadTags() {
    try {
        const tags = await api.getTags(libraryState.tagSortBy);
        libraryState.tags = tags;
        renderTagPanel();
    } catch (e) {
        console.error('loadTags error:', e);
    }
}

function renderTagPanel() {
    const container = $('tag-filter-container');
    if (!container) return;
    container.innerHTML = '';

    const searchTerm = libraryState.tagSearch.toLowerCase();
    const filteredTags = (libraryState.tags || []).filter(t =>
        t.name.toLowerCase().includes(searchTerm)
    );

    filteredTags.forEach(tagObj => {
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
            // Reset library and reload with new filters
            $('novel-grid').innerHTML = '';
            renderLibrary(true);
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

function chapterSectionId(chapterId) {
    return `ch-section-${chapterId}`;
}

function createChapterPlaceholder(chapterId) {
    const section = document.createElement('section');
    section.className = 'chapter-section';
    section.id = chapterSectionId(chapterId);
    section.dataset.chapterId = String(chapterId);
    section.dataset.loaded = 'false';
    section.style.minHeight = '200px';
    return section;
}

async function loadChapterSection(chapterId) {
    const sectionId = chapterSectionId(chapterId);
    let section = document.getElementById(sectionId);

    if (!section) {
        section = createChapterPlaceholder(chapterId);
        $('reading-column').appendChild(section);
    }

    if (section.dataset.loaded === 'true') return null;

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

    const sentinel = section.querySelector('.chapter-sentinel');
    if (infiniteScroll.observer && sentinel) {
        infiniteScroll.observer.observe(sentinel);
    }

    infiniteScroll.loadedIds.add(chapterId);
    return chapter;
}

function unloadChapterSection(chapterId) {
    const section = document.getElementById(chapterSectionId(chapterId));
    if (!section || section.dataset.loaded !== 'true') return;

    const height = section.offsetHeight;
    section.innerHTML = '';
    section.style.minHeight = `${height}px`;
    section.dataset.loaded = 'false';
    infiniteScroll.loadedIds.delete(chapterId);
}

async function updateChapterWindow(currentChapterId) {
    const ids = infiniteScroll.chapterIds;
    const currentIndex = ids.indexOf(currentChapterId);
    if (currentIndex === -1) return;

    const windowStart = Math.max(0, currentIndex - CHAPTER_WINDOW);
    const windowEnd = Math.min(ids.length - 1, currentIndex + CHAPTER_WINDOW);
    const windowSet = new Set(ids.slice(windowStart, windowEnd + 1));

    for (let i = windowStart; i <= windowEnd; i++) {
        const id = ids[i];
        if (!document.getElementById(chapterSectionId(id))) {
            const section = createChapterPlaceholder(id);
            const col = $('reading-column');
            const existingSections = [...col.querySelectorAll('.chapter-section')];
            const nextSection = existingSections.find(s => ids.indexOf(Number(s.dataset.chapterId)) > i);
            if (nextSection) col.insertBefore(section, nextSection);
            else col.appendChild(section);
        }
    }

    const loadPromises = [];
    for (let i = windowStart; i <= windowEnd; i++) {
        loadPromises.push(loadChapterSection(ids[i]));
    }
    await Promise.all(loadPromises);

    for (const loadedId of [...infiniteScroll.loadedIds]) {
        if (!windowSet.has(loadedId)) {
            unloadChapterSection(loadedId);
        }
    }
}

async function onChapterVisible(chapterId) {
    if (currentState.chapter && currentState.chapter.id === chapterId) return;

    try {
        const chapter = await api.getChapter(chapterId);
        currentState.chapter = chapter;

        $('reader-title').textContent = `${currentState.novel ? currentState.novel.title + ' > ' : ''}${chapter.chapter_title}`;

        if (currentState.novel) {
            const idx = currentState.novel.chapters.findIndex(c => c.id === chapterId);
            $('chapter-index-info').textContent = `Chapter ${idx + 1} of ${currentState.novel.chapters.length}`;
        }

        updateNavButtons(chapter);
        updateBookmarkIcon();
        updateNoteIcon();

        api.updateProgress({
            novel_id: chapter.novel_id,
            chapter_id: chapterId,
            scroll_position: 0.01
        }).catch(() => {});

        await updateChapterWindow(chapterId);

    } catch (e) {
        console.error('onChapterVisible error:', e);
    }
}

function updateNavButtons(chapter) {
    const goTo = (targetId) => {
        if (!targetId) return;
        const section = document.getElementById(chapterSectionId(targetId));
        if (section) {
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
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

async function initReader(startChapterId) {
    if (infiniteScroll.observer) {
        infiniteScroll.observer.disconnect();
        infiniteScroll.observer = null;
    }
    infiniteScroll.loadedIds.clear();
    infiniteScroll.chapterIds = [];

    const col = $('reading-column');
    col.innerHTML = '';

    if (!currentState.novel) {
        try {
            const chapter = await api.getChapter(startChapterId);
            currentState.novel = await api.getNovel(chapter.novel_id);
        } catch (e) {
            console.error('initReader: could not load novel context', e);
            return;
        }
    }

    infiniteScroll.chapterIds = (currentState.novel.chapters || []).map(c => c.id);

    infiniteScroll.observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (entry.isIntersecting) {
                const chId = Number(entry.target.dataset.chapterId);
                onChapterVisible(chId);
                break;
            }
        }
    }, {
        rootMargin: '0px 0px -70% 0px',
        threshold: 0
    });

    await updateChapterWindow(startChapterId);

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

// Tag Panel toggle
$('tag-panel-toggle').onclick = () => {
    const panel = $('tag-panel');
    panel.classList.toggle('hidden');
};
$('close-tag-panel').onclick = () => hide($('tag-panel'));

// Tag search
$('tag-search-input').oninput = debounce((e) => {
    libraryState.tagSearch = e.target.value;
    renderTagPanel();
}, 150);

// Tag sort buttons
$('tag-sort-count').onclick = () => {
    libraryState.tagSortBy = 'count';
    $('tag-sort-count').classList.add('active');
    $('tag-sort-alpha').classList.remove('active');
    loadTags();
};
$('tag-sort-alpha').onclick = () => {
    libraryState.tagSortBy = 'name';
    $('tag-sort-alpha').classList.add('active');
    $('tag-sort-count').classList.remove('active');
    loadTags();
};

// Search Panel toggle
$('search-panel-toggle').onclick = () => {
    const panel = $('search-panel');
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) {
        setTimeout(() => $('search-panel-input').focus(), 100);
    }
};
$('close-search-panel').onclick = () => hide($('search-panel'));

// Search panel input
$('search-panel-input').oninput = debounce(async (e) => {
    const q = e.target.value;
    if (q.length < 2) {
        $('search-novel-results').innerHTML = '';
        $('search-chapter-results').innerHTML = '';
        return;
    }
    const results = await api.search(q);
    $('search-novel-results').innerHTML = results.novels.map(n =>
        `<div class="search-result-item" onclick="window.app.navToNovel(${n.id})">${n.title}</div>`
    ).join('');
    $('search-chapter-results').innerHTML = results.chapters.map(c =>
        `<div class="search-result-item" onclick="window.app.navToChapter(${c.id})">${c.chapter_title}</div>`
    ).join('');
}, 300);

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
$('sort-select').onchange = (e) => {
    currentState.filter.sortBy = e.target.value;
    $('novel-grid').innerHTML = '';
    renderLibrary(true);
};

// Library search — filters the already-loaded novels client-side
$('library-search').oninput = debounce((e) => {
    const q = e.target.value.toLowerCase().trim();
    const cards = document.querySelectorAll('.novel-card');
    cards.forEach(card => {
        const title = card.querySelector('h3')?.textContent?.toLowerCase() || '';
        const author = card.querySelector('p')?.textContent?.toLowerCase() || '';
        if (!q || title.includes(q) || author.includes(q)) {
            card.style.display = '';
        } else {
            card.style.display = 'none';
        }
    });
}, 200);

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

// Expose for inline onclick handlers
window.app = {
    navToNovel: (id) => { hide($('search-panel')); navigateTo(VIEWS.NOVEL, { id }); },
    navToChapter: (id) => { hide($('search-panel')); navigateTo(VIEWS.READER, { id }); }
};

// Keyboard Shortcuts
window.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        const panel = $('search-panel');
        panel.classList.toggle('hidden');
        if (!panel.classList.contains('hidden')) {
            setTimeout(() => $('search-panel-input').focus(), 100);
        }
    }

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
        if (e.key === 'Escape') {
            hide($('settings-panel'));
            hide($('notes-panel'));
            hide($('search-panel'));
            hide($('tag-panel'));
        }
    }
};

// Initialization
loadSettings();
initPopstateHandler();

// Check initial URL hash for deep linking
const hash = window.location.hash;
if (hash.startsWith('#novel/')) {
    const novelId = parseInt(hash.split('/')[1]);
    if (novelId) navigateTo(VIEWS.NOVEL, { id: novelId }, false);
} else if (hash.startsWith('#reader/')) {
    const chapterId = parseInt(hash.split('/')[1]);
    if (chapterId) navigateTo(VIEWS.READER, { id: chapterId }, false);
} else {
    navigateTo(VIEWS.LIBRARY, {}, false);
}
