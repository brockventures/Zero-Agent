/**
 * Mirrormere Calendar Family Widget Controller
 * Manages 3-way view switching (Day | Week | Month), active view state synchronization,
 * tap-to-expand event detail modal, carousel rotation pausing, 60-second inactivity reset timer,
 * and horizontal touch swipe gesture navigation with window bounds paging.
 */
(() => {

  function detectEventFlair(title) {
    if (!title || typeof title !== 'string') return null;
    const t = title.trim();
    if (!t) return null;

    if (/\b(birthday|bday|b-day)\b/i.test(t)) return 'birthday';
    if (/\b(party|celebration)\b/i.test(t)) return 'party';
    if (/\b(dentist|orthodontist|dental|doctor|pediatric)\b/i.test(t)) return 'dentist';
    if (/\b(movie|cinema|film)\b/i.test(t)) return 'movie';
    if (/\b(flight|airline|airport|fly)\b/i.test(t)) return 'flight';
    if (/\b(soccer|practice|game|basketball|baseball)\b/i.test(t)) return 'sports';
    if (
      /\b(veterans day|thanksgiving|christmas|new year|memorial day|labor day|independence day|4th of july|easter|halloween|valentine|mother's day|father's day|mlk|presidents day|juneteenth|holiday)\b/i.test(
        t,
      )
    ) {
      return 'holiday';
    }

    return null;
  }

  function normalizeCustomMonthDay(raw) {
    if (typeof raw !== 'string') return null;
    const trimmed = raw.trim();
    const fullMatch = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(trimmed);
    if (fullMatch) {
      return `${fullMatch[2].padStart(2, '0')}-${fullMatch[3].padStart(2, '0')}`;
    }
    const mdMatch = /^(\d{1,2})[-/](\d{1,2})$/.exec(trimmed);
    if (mdMatch) {
      return `${mdMatch[1].padStart(2, '0')}-${mdMatch[2].padStart(2, '0')}`;
    }
    return null;
  }

  function formatCustomSpecialDayBadge(nameRaw, labelRaw) {
    if (typeof labelRaw === 'string' && labelRaw.trim()) {
      const cleanLabel = labelRaw.trim().replace(/^🎂\s*/, '');
      return `🎂 ${cleanLabel}`;
    }
    if (typeof nameRaw === 'string' && nameRaw.trim()) {
      const cleanName = nameRaw.trim();
      if (/bday|birthday/i.test(cleanName)) {
        return `🎂 ${cleanName.replace(/^🎂\s*/, '')}`;
      }
      return `🎂 ${cleanName}'s Bday`;
    }
    return null;
  }

  function matchCustomEntryObject(entry, targetMonthDay, fallbackKey) {
    let entryMonthDay = normalizeCustomMonthDay(entry.date != null ? entry.date : entry.month_day);
    if (!entryMonthDay && typeof entry.month === 'number' && typeof entry.day === 'number') {
      entryMonthDay = `${String(entry.month).padStart(2, '0')}-${String(entry.day).padStart(2, '0')}`;
    }
    if (!entryMonthDay && fallbackKey) {
      entryMonthDay = normalizeCustomMonthDay(fallbackKey);
    }
    if (entryMonthDay !== targetMonthDay) return null;

    const rawName =
      typeof entry.name === 'string'
        ? entry.name
        : typeof entry.key === 'string'
          ? entry.key
          : typeof entry.id === 'string'
            ? entry.id
            : fallbackKey && !normalizeCustomMonthDay(fallbackKey)
              ? fallbackKey
              : '';
    const rawLabel =
      typeof entry.label === 'string'
        ? entry.label
        : typeof entry.banner_label === 'string'
          ? entry.banner_label
          : undefined;
    return formatCustomSpecialDayBadge(rawName, rawLabel);
  }

  function resolveSingleSpecialSource(dateStr, source) {
    if (!source || typeof source === 'boolean') return null;
    const targetMonthDay = normalizeCustomMonthDay(dateStr);
    if (!targetMonthDay) return null;

    if (Array.isArray(source)) {
      for (const item of source) {
        if (item && typeof item === 'object' && !Array.isArray(item)) {
          const match = matchCustomEntryObject(item, targetMonthDay);
          if (match) return match;
        }
      }
      return null;
    }

    if (typeof source === 'object') {
      for (const [k, v] of Object.entries(source)) {
        if (typeof v === 'string') {
          const kAsDate = normalizeCustomMonthDay(k);
          const vAsDate = normalizeCustomMonthDay(v);
          if (vAsDate === targetMonthDay) {
            const match = formatCustomSpecialDayBadge(k);
            if (match) return match;
          } else if (kAsDate === targetMonthDay) {
            const match = formatCustomSpecialDayBadge(v);
            if (match) return match;
          }
        } else if (v && typeof v === 'object' && !Array.isArray(v)) {
          const match = matchCustomEntryObject(v, targetMonthDay, k);
          if (match) return match;
        }
      }
    }
    return null;
  }

  function resolveConfiguredSpecialDayLabel(dateStr, birthdays, specialDays) {
    if (!dateStr) return null;
    return (
      resolveSingleSpecialSource(dateStr, birthdays) ||
      resolveSingleSpecialSource(dateStr, specialDays)
    );
  }

  function detectDaySpecialStatus(dayEvents, members) {
    if (!Array.isArray(dayEvents) || dayEvents.length === 0) return null;
    const safeMembers = Array.isArray(members) ? members : [];

    // 1. Family Birthday
    for (const ev of dayEvents) {
      if (!ev) continue;
      const title = (ev.title || ev.eventTitle || '').trim();
      if (!title) continue;

      if (/\b(birthday|bday|b-day)\b/i.test(title)) {
        if (
          /\(work\)|\bcoworker\b/i.test(title) ||
          /\bwork\b/i.test(title) ||
          (Array.isArray(ev.owners) && ev.owners.some((o) => /\(work\)|\bcoworker\b|\bwork\b/i.test(o))) ||
          (ev.description && /\bcoworker\b/i.test(ev.description))
        ) {
          continue;
        }

        let isFamily = false;
        for (const m of safeMembers) {
          if (!m || !m.name) continue;
          const name = m.name.trim();
          if (!name) continue;

          const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          const nameRegex = new RegExp(`\\b${escaped}('s)?\\b`, 'i');
          if (nameRegex.test(title)) {
            isFamily = true;
            break;
          }

          if (
            Array.isArray(ev.owners) &&
            ev.owners.some(
              (o) =>
                o.toLowerCase() === name.toLowerCase() ||
                o.toLowerCase().includes(name.toLowerCase()),
            )
          ) {
            isFamily = true;
            break;
          }

          if (m.initial && Array.isArray(ev.initials) && ev.initials.includes(m.initial)) {
            isFamily = true;
            break;
          }

          if (ev.member && ev.member.toLowerCase() === name.toLowerCase()) {
            isFamily = true;
            break;
          }
        }

        if (isFamily) {
          return 'birthday';
        }
      }
    }

    // 2. Holiday
    for (const ev of dayEvents) {
      if (!ev) continue;
      const title = (ev.title || ev.eventTitle || '').trim();
      if (
        /\b(veterans day|thanksgiving|christmas|new year|memorial day|labor day|independence day|4th of july|easter|halloween|valentine|mother's day|father's day|mlk|presidents day|juneteenth|holiday)\b/i.test(
          title,
        )
      ) {
        return 'holiday';
      }
      const cal = ev.calendar || ev.calendar_name || '';
      if (cal && /holiday/i.test(cal)) {
        return 'holiday';
      }
    }

    return null;
  }

  function getEventFlairWatermarkSvg(flair) {
    switch (flair) {
      case 'birthday':
      case 'party':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14c-2.76 0-5-2.69-5-6a5 5 0 0 1 10 0c0 3.31-2.24 6-5 6Z" fill="currentColor" fill-opacity="0.25"/><path d="m8 14-1 2h2l-1-2"/><path d="M8 16c-.5 2-2 3-1 5"/><path d="M15 12c2.21 0 4-2.24 4-5a4 4 0 0 0-8 0" stroke-dasharray="2 2"/></svg>';
      case 'dentist':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3C4.5 3 3 5 3 7.5c0 3 1.5 6 2 9.5.5 3 2 4.5 3.5 4.5 1 0 1.5-1 2-3 .5-2 1.5-2 2-2s1.5 0 2 2c.5 2 1 3 2 3 1.5 0 3-1.5 3.5-4.5.5-3.5 2-6.5 2-9.5C22 5 20.5 3 18 3c-2 0-3.5 1.5-5.5 1.5S9 3 7 3Z" fill="currentColor" fill-opacity="0.25"/></svg>';
      case 'movie':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10h10l-1.5 11h-7L7 10Z" fill="currentColor" fill-opacity="0.25"/><path d="M10 10v11M14 10v11"/><path d="M6 10c-.8-1.5.5-3.5 2-3.5 1 0 1.5.5 2 1 .5-.5 1-1 2-1s1.5.5 2 1c.5-.5 1-1 2-1 1.5 0 2.8 2 2 3.5"/></svg>';
      case 'flight':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 20.5 3c-1-1-3-.5-4.5 1L12.5 7.5 4.3 5.7c-.7-.1-1.3.3-1.4 1-.1.5.2 1 .6 1.3l6 4.5-3 3-2.5-.5c-.4-.1-.8.1-1 .4-.3.4-.2.9.2 1.2l2.5 1.8 1.8 2.5c.3.4.8.5 1.2.2.3-.2.5-.6.4-1l-.5-2.5 3-3 4.5 6c.3.4.8.7 1.3.6.7-.1 1.1-.7 1-1.4l-1.7-8.2Z" fill="currentColor" fill-opacity="0.25"/></svg>';
      case 'sports':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" fill="currentColor" fill-opacity="0.25"/><path d="m12 7 3.5 2.5v4L12 16l-3.5-2.5v-4L12 7Z"/><path d="M12 7V3M15.5 9.5l3.5-2M15.5 13.5l3.5 2M12 16v4M8.5 13.5l-3.5 2M8.5 9.5l-3.5-2"/></svg>';
      case 'holiday':
        return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" fill="currentColor" fill-opacity="0.25"/></svg>';
      default:
        return '';
    }
  }

  function getModalFlairBannerSvg(flair) {
    switch (flair) {
      case 'birthday':
      case 'party':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><defs><linearGradient id="bgB1_js" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#f472b6"/><stop offset="100%" stop-color="#db2777"/></linearGradient><linearGradient id="bgB2_js" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#38bdf8"/><stop offset="100%" stop-color="#0284c7"/></linearGradient><linearGradient id="bgB3_js" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#facc15"/><stop offset="100%" stop-color="#eab308"/></linearGradient></defs><circle cx="30" cy="30" r="3" fill="#ffffff" opacity="0.8"/><circle cx="80" cy="20" r="2.5" fill="#fde047" opacity="0.9"/><circle cx="140" cy="40" r="3.5" fill="#67e8f9" opacity="0.75"/><circle cx="280" cy="25" r="3" fill="#fde047" opacity="0.85"/><circle cx="340" cy="35" r="2.5" fill="#ffffff" opacity="0.8"/><circle cx="370" cy="70" r="3" fill="#f472b6" opacity="0.9"/><path d="M50 70 L52 76 L58 77 L53 81 L55 87 L50 83 L45 87 L47 81 L42 77 L48 76 Z" fill="#ffffff" opacity="0.75"/><path d="M320 80 L322 84 L326 85 L323 88 L324 92 L320 89 L316 92 L317 88 L314 85 L318 84 Z" fill="#fde047" opacity="0.8"/><path d="M185 85 Q 180 105 170 120" stroke="rgba(255,255,255,0.6)" stroke-width="1.5" fill="none"/><path d="M200 90 Q 200 110 205 120" stroke="rgba(255,255,255,0.6)" stroke-width="1.5" fill="none"/><path d="M215 85 Q 220 105 230 120" stroke="rgba(255,255,255,0.6)" stroke-width="1.5" fill="none"/><ellipse cx="178" cy="52" rx="20" ry="26" fill="url(#bgB2_js)"/><ellipse cx="173" cy="44" rx="4" ry="7" fill="#ffffff" opacity="0.4" transform="rotate(-15 173 44)"/><polygon points="175,77 181,77 178,82" fill="#0284c7"/><ellipse cx="222" cy="52" rx="20" ry="26" fill="url(#bgB3_js)"/><ellipse cx="217" cy="44" rx="4" ry="7" fill="#ffffff" opacity="0.4" transform="rotate(-15 217 44)"/><polygon points="219,77 225,77 222,82" fill="#ca8a04"/><ellipse cx="200" cy="45" rx="22" ry="28" fill="url(#bgB1_js)"/><ellipse cx="194" cy="36" rx="5" ry="8" fill="#ffffff" opacity="0.5" transform="rotate(-15 194 36)"/><polygon points="197,72 203,72 200,77" fill="#be185d"/></svg>';
      case 'dentist':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><circle cx="80" cy="40" r="14" fill="#ffffff" opacity="0.15"/><circle cx="120" cy="80" r="8" fill="#ffffff" opacity="0.2"/><circle cx="280" cy="35" r="10" fill="#ffffff" opacity="0.15"/><circle cx="320" cy="75" r="16" fill="#ffffff" opacity="0.2"/><path d="M150 40 L152 46 L158 48 L152 50 L150 56 L148 50 L142 48 L148 46 Z" fill="#ffffff" opacity="0.9"/><path d="M245 35 L247 40 L252 41 L247 43 L245 48 L243 43 L238 41 L243 40 Z" fill="#ffffff" opacity="0.9"/><path d="M235 80 L236 83 L239 84 L236 85 L235 88 L234 85 L231 84 L234 83 Z" fill="#ffffff" opacity="0.8"/><path d="M190 28 C 172 28 165 42 165 56 C 165 72 172 88 178 102 C 182 110 188 108 192 98 C 196 88 198 88 200 88 C 202 88 204 88 208 98 C 212 108 218 110 222 102 C 228 88 235 72 235 56 C 235 42 228 28 210 28 C 204 28 202 33 200 33 C 198 33 196 28 190 28 Z" fill="#ffffff"/><path d="M182 45 C 178 50 178 60 180 66" stroke="#06b6d4" stroke-width="2.5" stroke-linecap="round" fill="none" opacity="0.6"/></svg>';
      case 'movie':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><circle cx="60" cy="30" r="2.5" fill="#fde047" opacity="0.8"/><circle cx="120" cy="50" r="3" fill="#ffffff" opacity="0.7"/><circle cx="280" cy="40" r="2" fill="#ffffff" opacity="0.8"/><circle cx="340" cy="25" r="3.5" fill="#fde047" opacity="0.9"/><g transform="translate(170, 20)"><circle cx="20" cy="22" r="9" fill="#fef08a"/><circle cx="34" cy="18" r="10" fill="#facc15"/><circle cx="48" cy="24" r="9" fill="#fef08a"/><circle cx="12" cy="30" r="8" fill="#fde047"/><circle cx="28" cy="28" r="9" fill="#ffffff"/><circle cx="44" cy="32" r="8" fill="#facc15"/><circle cx="56" cy="30" r="7" fill="#fde047"/><polygon points="12,38 56,38 48,88 20,88" fill="#e11d48"/><polygon points="19,38 27,38 25,88 21,88" fill="#ffffff"/><polygon points="33,38 41,38 39,88 35,88" fill="#ffffff"/></g></svg>';
      case 'flight':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><path d="M40 90 A 18 18 0 0 1 70 80 A 24 24 0 0 1 110 85 A 14 14 0 0 1 125 95 Z" fill="#ffffff" opacity="0.25"/><path d="M260 85 A 16 16 0 0 1 290 75 A 22 22 0 0 1 325 80 A 16 16 0 0 1 345 92 Z" fill="#ffffff" opacity="0.3"/><path d="M80 85 Q 140 65 190 55" stroke="#ffffff" stroke-width="2.5" stroke-dasharray="6 4" stroke-linecap="round" fill="none" opacity="0.65"/><g transform="translate(185, 25) rotate(-15)"><path d="M35 15 L12 2 L8 2 L14 15 L2 15 L0 12 L-4 12 L-2 18 L-4 24 L0 24 L2 21 L14 21 L8 34 L12 34 L35 21 C42 19 42 17 35 15 Z" fill="#ffffff"/></g></svg>';
      case 'sports':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><path d="M40 100 L360 100" stroke="#ffffff" stroke-width="1.5" opacity="0.3"/><path d="M200 60 L200 100" stroke="#ffffff" stroke-width="1.5" opacity="0.25"/><polygon points="120,30 123,37 130,37 124,42 126,49 120,45 114,49 116,42 110,37 117,37" fill="#fde047" opacity="0.8"/><polygon points="280,30 283,37 290,37 284,42 286,49 280,45 274,49 276,42 270,37 277,37" fill="#fde047" opacity="0.8"/><g transform="translate(175, 25)"><circle cx="25" cy="25" r="24" fill="#ffffff"/><polygon points="25,12 34,18 30,28 20,28 16,18" fill="#1e293b"/><line x1="25" y1="12" x2="25" y2="2" stroke="#1e293b" stroke-width="2"/><line x1="34" y1="18" x2="44" y2="14" stroke="#1e293b" stroke-width="2"/><line x1="30" y1="28" x2="38" y2="36" stroke="#1e293b" stroke-width="2"/><line x1="20" y1="28" x2="12" y2="36" stroke="#1e293b" stroke-width="2"/><line x1="16" y1="18" x2="6" y2="14" stroke="#1e293b" stroke-width="2"/></g></svg>';
      case 'holiday':
        return '<svg viewBox="0 0 400 120" style="width:100%;height:100%;" preserveAspectRatio="xMidYMid slice"><circle cx="60" cy="40" r="3" fill="#ffffff" opacity="0.8"/><circle cx="100" cy="20" r="2" fill="#ffffff" opacity="0.9"/><circle cx="300" cy="30" r="3.5" fill="#ffffff" opacity="0.85"/><circle cx="350" cy="50" r="2" fill="#ffffff" opacity="0.75"/><path d="M200 20 L203 35 L218 38 L203 41 L200 56 L197 41 L182 38 L197 35 Z" fill="#ffffff"/><path d="M150 45 L152 54 L161 56 L152 58 L150 67 L148 58 L139 56 L148 54 Z" fill="#fde047" opacity="0.9"/><path d="M250 45 L252 54 L261 56 L252 58 L250 67 L248 58 L239 56 L248 54 Z" fill="#fde047" opacity="0.9"/></svg>';
      default:
        return '';
    }
  }

  class CalendarFamilyInstance {
    constructor(element, options = {}) {
      this.element = element;
      this.options = options;
      this.widgetId = element.dataset.widgetId || element.dataset.widgetType || 'calendar-grid';
      this.data = this.parseData();
      this.config = this.parseConfig();
      this.eventIndex = this.buildEventIndex();
      this.defaultView = this.resolveDefaultView();
      this.activeView = this.resolveActiveView();
      this.activeModal = null;
      this.inactivityTimeout = typeof options.inactivityTimeout === 'number' ? options.inactivityTimeout : 60000;
      this.inactivityTimer = null;

      // Date state & window bounds
      this.baseDate = this.initBaseDate();
      this.currentDate = new Date(this.baseDate.getTime());
      this.baseWeekStart = this.initBaseWeekStart();
      this.currentWeekStart = new Date(this.baseWeekStart.getTime());
      this.pageOffset = 0;
      this.cachedRange = this.resolveCachedRange();
      this.syncIndicatorTimer = null;

      this.fetchFn = options.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : (typeof fetch !== 'undefined' ? fetch : null));
      this.renderCache = new Map();
      this.renderSeq = 0;
      this.abortController = null;

      // Touch swipe configuration & state
      this.touchStartX = 0;
      this.touchStartY = 0;
      this.touchCurrentX = 0;
      this.touchCurrentY = 0;
      this.touchStartTime = 0;
      this.isSwiping = false;
      this.justSwiped = false;
      this.swipeDistanceThreshold = typeof options.swipeDistanceThreshold === 'number' ? options.swipeDistanceThreshold : 40;
      this.swipeVelocityThreshold = typeof options.swipeVelocityThreshold === 'number' ? options.swipeVelocityThreshold : 0.15;

      this.prevBtn = element.querySelector('.cf-nav-btn.cf-prev-btn');
      this.nextBtn = element.querySelector('.cf-nav-btn.cf-next-btn');
      this.viewButtons = Array.from(element.querySelectorAll('.cf-view-btn'));
      this.viewPanels = {
        week: element.querySelector('.cf-view-panel.cf-view-week') || element.querySelector('.cf-view-week'),
        month: element.querySelector('.cf-view-panel.cf-view-month') || element.querySelector('.cf-view-month'),
      };
      this.labels = {
        week: element.querySelector('.cf-range-label.cf-label-week'),
        month: element.querySelector('.cf-range-label.cf-label-month'),
      };
      this.todayBadge = element.querySelector('.cf-today-badge');

      this.clickHandler = this.handleClick.bind(this);
      this.activityHandler = this.handleUserActivity.bind(this);
      this.keydownHandler = this.handleKeyDown.bind(this);
      this.touchStartHandler = this.handleTouchStart.bind(this);
      this.touchMoveHandler = this.handleTouchMove.bind(this);
      this.touchEndHandler = this.handleTouchEnd.bind(this);
      this.touchCancelHandler = this.handleTouchCancel.bind(this);

      this.init();
      this.decorateMealieEvents();
      this.applySpecialDayHighlights();
      this.applyEventFlairs();
    }

    decorateMealieEvents() {
      if (!this.element || typeof this.element.querySelectorAll !== 'function') return;
      const pills = this.element.querySelectorAll(
        '.cf-mealie-bar, .cf-mealie-card, .cf-allday[data-event-location^="/hearth-api/mealie/image/"], .cf-allday[data-event-image-url]'
      );
      const isMealieImgUrl = (u) => Boolean(u && (
        u.startsWith('/hearth-api/mealie/image/') ||
        u.includes('/api/media/recipes/') ||
        u.startsWith('data:image/')
      ));
      pills.forEach(pill => {
        if (pill.dataset.mealieDecorated === 'true') return;
        const imgUrl = (pill.dataset && (pill.dataset.eventImageUrl || pill.dataset.eventLocation)) || '';
        const rawTitle = ((pill.dataset && pill.dataset.eventTitle) || pill.textContent || '').replace(/^🍽️\s*/, '').trim();
        const isBottomBar = pill.classList && typeof pill.classList.contains === 'function' && (
          pill.classList.contains('cf-mealie-bar') || pill.classList.contains('cf-mealie-card')
        );
        pill.dataset.mealieDecorated = 'true';
        pill.textContent = '';

        if (isBottomBar) {
          pill.style.position = 'relative';
          pill.style.minHeight = '128px';
          pill.style.height = '128px';
          pill.style.borderRadius = '12px';
          pill.style.overflow = 'hidden';
          pill.style.display = 'flex';
          pill.style.flexDirection = 'column';
          pill.style.justifyContent = 'space-between';
          pill.style.padding = '0';
          pill.style.background = '#d97757';
          pill.style.backgroundColor = '#d97757';
          pill.style.color = '#ffffff';

          if (isMealieImgUrl(imgUrl)) {
            const imgEl = document.createElement('img');
            imgEl.src = imgUrl;
            imgEl.alt = '';
            imgEl.className = 'cf-mealie-photo';
            imgEl.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;z-index:0;';
            pill.appendChild(imgEl);
          }

          const badgeEl = document.createElement('span');
          badgeEl.className = 'cf-mealie-pill';
          badgeEl.style.cssText = 'position:absolute;top:6px;left:6px;z-index:2;padding:2px 7px;border-radius:9999px;font-size:0.56rem;font-weight:800;letter-spacing:0.02em;color:#ffffff;background:rgba(16,12,10,0.52);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);line-height:1.2;pointer-events:none;';
          badgeEl.textContent = '🍽️ Dinner';
          pill.appendChild(badgeEl);

          const scrimEl = document.createElement('div');
          scrimEl.className = 'cf-mealie-scrim';
          scrimEl.style.cssText = 'position:absolute;left:0;right:0;bottom:0;z-index:1;padding:20px 8px 7px;background:linear-gradient(to top, rgba(16,12,10,0.90) 0%, rgba(16,12,10,0.62) 55%, rgba(16,12,10,0.0) 100%);display:flex;align-items:flex-end;pointer-events:none;';
          const spanEl = document.createElement('span');
          spanEl.className = 'cf-mealie-title';
          spanEl.style.cssText = 'font-size:0.70rem;font-weight:800;line-height:1.2;color:#ffffff;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;text-shadow:0 1px 2px rgba(0,0,0,0.6);';
          spanEl.textContent = rawTitle;
          scrimEl.appendChild(spanEl);
          pill.appendChild(scrimEl);
          return;
        }

        pill.style.display = 'flex';
        pill.style.alignItems = 'center';
        pill.style.gap = '6px';
        pill.style.padding = '3px 6px';
        if (isMealieImgUrl(imgUrl)) {
          const imgEl = document.createElement('img');
          imgEl.src = imgUrl;
          imgEl.alt = '';
          imgEl.style.cssText = 'width:26px;height:26px;border-radius:6px;object-fit:cover;flex-shrink:0;box-shadow:0 1px 3px rgba(0,0,0,0.3);';
          pill.appendChild(imgEl);
        }
        const spanEl = document.createElement('span');
        spanEl.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:800;';
        spanEl.textContent = `🍽️ ${rawTitle}`;
        pill.appendChild(spanEl);
      });
    }

    applySpecialDayHighlights() {
      if (!this.element || typeof this.element.querySelectorAll !== 'function') return;
      const isSpecialDaysEnabled = this.config.celebrate_special_days !== false &&
        (this.element ? this.element.dataset.celebrateSpecialDays !== 'false' : true);

      const dayEls = this.element.querySelectorAll('.cf-day, .cf-month-day');
      if (!isSpecialDaysEnabled) {
        dayEls.forEach(dayEl => {
          if (dayEl.classList) {
            dayEl.classList.remove('cf-special-birthday', 'cf-special-holiday');
          }
          if (dayEl.dataset) {
            delete dayEl.dataset.specialDay;
          }
          const pill = dayEl.querySelector ? dayEl.querySelector('.cf-special-pill') : null;
          if (pill) {
            if (typeof pill.remove === 'function') pill.remove();
            else if (pill.parentNode) pill.parentNode.removeChild(pill);
          }
        });
        return;
      }

      const members = Array.isArray(this.data.members)
        ? this.data.members
        : (Array.isArray(this.config && this.config.members) ? this.config.members : []);
      dayEls.forEach(dayEl => {
        const date = dayEl.dataset ? dayEl.dataset.date : null;
        let dayEvents = [];
        let matchedDay = null;

        if (date && Array.isArray(this.data.days)) {
          const dayObj = this.data.days.find(d => d.date === date);
          if (dayObj) {
            matchedDay = dayObj;
            dayEvents.push(
              ...(dayObj.all_day || []),
              ...(dayObj.events || []),
              ...(dayObj.early_events || []),
              ...(dayObj.late_events || []),
              ...(dayObj.meals || [])
            );
          }
        }
        if (date && Array.isArray(this.data.month_days)) {
          const mDayObj = this.data.month_days.find(d => d.date === date);
          if (mDayObj) {
            if (!matchedDay) matchedDay = mDayObj;
            if (Array.isArray(mDayObj.events) && mDayObj.events.length > 0) {
              dayEvents.push(...mDayObj.events);
            } else if (mDayObj.event_title) {
              dayEvents.push({ id: date, title: mDayObj.event_title });
            }
          }
        }

        // Also inspect child event elements
        if (typeof dayEl.querySelectorAll === 'function') {
          const cardEls = dayEl.querySelectorAll('.cf-day-event, .cf-event, .cf-edge-event, .cf-month-event-title');
          cardEls.forEach(cardEl => {
            const evData = this.resolveEventData(cardEl);
            if (evData) dayEvents.push(evData);
          });
        }

        const configuredBdayLabel = date
          ? resolveConfiguredSpecialDayLabel(
              date,
              this.config && this.config.birthdays,
              this.config && this.config.special_days,
            )
          : null;
        const eventStatus = detectDaySpecialStatus(dayEvents, members);
        const status = configuredBdayLabel ? 'birthday' : eventStatus;
        if (dayEl.classList) {
          dayEl.classList.remove('cf-special-birthday', 'cf-special-holiday');
        }
        const existingPill = dayEl.querySelector ? dayEl.querySelector('.cf-special-pill') : null;
        if (existingPill) {
          if (typeof existingPill.remove === 'function') existingPill.remove();
          else if (existingPill.parentNode) existingPill.parentNode.removeChild(existingPill);
        }

        if (status === 'birthday') {
          if (dayEl.classList) dayEl.classList.add('cf-special-birthday');
          if (dayEl.dataset) dayEl.dataset.specialDay = 'birthday';
          const header = dayEl.querySelector ? dayEl.querySelector('.cf-day-header, .cf-month-day-header') : null;
          if (header && typeof document !== 'undefined' && document.createElement) {
            const pill = document.createElement('span');
            pill.className = 'cf-special-pill cf-pill-birthday';
            pill.textContent = (matchedDay && matchedDay.special_label)
              ? ('🎂 ' + matchedDay.special_label)
              : (configuredBdayLabel || '🎂 Family Bday');
            header.appendChild(pill);
          }
        } else if (status === 'holiday') {
          if (dayEl.classList) dayEl.classList.add('cf-special-holiday');
          if (dayEl.dataset) dayEl.dataset.specialDay = 'holiday';
          const header = dayEl.querySelector ? dayEl.querySelector('.cf-day-header, .cf-month-day-header') : null;
          if (header && typeof document !== 'undefined' && document.createElement) {
            const pill = document.createElement('span');
            pill.className = 'cf-special-pill cf-pill-holiday';
            pill.textContent = '✨ Holiday';
            header.appendChild(pill);
          }
        } else if (dayEl.dataset) {
          delete dayEl.dataset.specialDay;
        }
      });
    }

    applyEventFlairs() {
      if (!this.element || typeof this.element.querySelectorAll !== 'function') return;
      const isEventFlairEnabled = this.config.event_flair !== false &&
        (this.element ? this.element.dataset.eventFlair !== 'false' : true);

      const eventCards = this.element.querySelectorAll('.cf-day-event, .cf-allday');
      eventCards.forEach(cardEl => {
        if (!isEventFlairEnabled) {
          const currentFlair = cardEl.dataset ? cardEl.dataset.flair : null;
          if (currentFlair && cardEl.classList) {
            cardEl.classList.remove(`cf-flair-${currentFlair}`);
          }
          if (cardEl.dataset) delete cardEl.dataset.flair;
          const wm = cardEl.querySelector ? cardEl.querySelector('.cf-event-flair-watermark') : null;
          if (wm) {
            if (typeof wm.remove === 'function') wm.remove();
            else if (wm.parentNode) wm.parentNode.removeChild(wm);
          }
          return;
        }

        const title = (cardEl.dataset && (cardEl.dataset.eventTitle || cardEl.dataset.title)) || (cardEl.querySelector && cardEl.querySelector('.cf-event-title')?.textContent) || cardEl.textContent?.trim();
        const flair = detectEventFlair(title);
        if (flair) {
          if (cardEl.classList) cardEl.classList.add(`cf-flair-${flair}`);
          if (cardEl.dataset) cardEl.dataset.flair = flair;
          if (!cardEl.querySelector || !cardEl.querySelector('.cf-event-flair-watermark')) {
            if (typeof document !== 'undefined' && document.createElement) {
              const wm = document.createElement('span');
              wm.className = 'cf-event-flair-watermark';
              wm.setAttribute('aria-hidden', 'true');
              wm.innerHTML = getEventFlairWatermarkSvg(flair);
              cardEl.appendChild(wm);
            }
          }
        }
      });
    }



    parseData() {
      const dataEl = this.element.querySelector('.calendar-grid-data, .calendar-family-data');
      if (dataEl && dataEl.textContent) {
        try {
          let parsed = JSON.parse(dataEl.textContent.trim());
          if (typeof parsed === 'string') {
            try {
              parsed = JSON.parse(parsed);
            } catch (_) {}
          }
          return parsed || {};
        } catch (e) {
          console.warn(`[MirrormereCalendarFamily] Failed to parse calendar data for ${this.widgetId}:`, e);
        }
      }
      return {};
    }

    parseConfig() {
      const baseCfg = (this.data && typeof this.data.config === 'object' && this.data.config) ? this.data.config : {};
      if (this.options && typeof this.options === 'object') {
        return Object.assign({}, baseCfg, this.options);
      }
      return baseCfg;
    }

    buildEventIndex() {
      const index = new Map();
      const addEv = (ev) => {
        if (!ev) return;
        if (ev.id) index.set(String(ev.id), ev);
        if (ev.title && !index.has(String(ev.title))) {
          index.set(String(ev.title), ev);
        }
      };

      // 0. Deduplicated flat event list if provided
      if (Array.isArray(this.data.events)) {
        for (const ev of this.data.events) addEv(ev);
      }

      // 1. Day view:
      if (Array.isArray(this.data.all_day_events)) {
        for (const ev of this.data.all_day_events) addEv(ev);
      }
      if (Array.isArray(this.data.day_all_day_events)) {
        for (const ev of this.data.day_all_day_events) addEv(ev);
      }
      if (Array.isArray(this.data.columns)) {
        for (const col of this.data.columns) {
          if (Array.isArray(col.events)) for (const ev of col.events) addEv(ev);
          if (Array.isArray(col.early_events)) for (const ev of col.early_events) addEv(ev);
          if (Array.isArray(col.late_events)) for (const ev of col.late_events) addEv(ev);
        }
      }

      // 2. Week view / Month view:
      if (Array.isArray(this.data.days)) {
        for (const day of this.data.days) {
          if (Array.isArray(day.all_day)) for (const ev of day.all_day) addEv(ev);
          if (Array.isArray(day.meals)) for (const ev of day.meals) addEv(ev);
          if (Array.isArray(day.timed)) for (const ev of day.timed) addEv(ev);
          if (Array.isArray(day.events)) for (const ev of day.events) addEv(ev);
          if (Array.isArray(day.early_events)) for (const ev of day.early_events) addEv(ev);
          if (Array.isArray(day.late_events)) for (const ev of day.late_events) addEv(ev);
        }
      }
      if (Array.isArray(this.data.month_days)) {
        for (const day of this.data.month_days) {
          if (Array.isArray(day.events)) for (const ev of day.events) addEv(ev);
        }
      }

      return index;
    }

    resolveDefaultView() {
      const raw = (this.element.dataset.defaultView || (this.config && this.config.default_view) || 'week').toLowerCase();
      if (raw === 'month') {
        return 'month';
      }
      return 'week';
    }

    resolveActiveView() {
      const raw = (this.element.dataset.activeView || this.defaultView).toLowerCase();
      if (raw === 'month') {
        return 'month';
      }
      return 'week';
    }

    initBaseDate() {
      let dateStr = (this.data && this.data.date) ||
                    (this.data && this.data.today) ||
                    (this.data && this.data.range_start) ||
                    (this.config && this.config.date);
      if (!dateStr && Array.isArray(this.data && this.data.days) && this.data.days.length > 0) {
        dateStr = this.data.days[0].date;
      }
      const parts = this.parseDateParts(dateStr);
      if (parts) {
        return this.createLocalDate(parts.year, parts.month, parts.day);
      }
      const now = new Date();
      return this.createLocalDate(now.getFullYear(), now.getMonth() + 1, now.getDate());
    }

    initBaseWeekStart() {
      let startStr = (this.data && this.data.range_start);
      if (!startStr && Array.isArray(this.data && this.data.days) && this.data.days.length > 0) {
        startStr = this.data.days[0].date;
      }
      const parts = this.parseDateParts(startStr);
      if (parts) {
        return this.createLocalDate(parts.year, parts.month, parts.day);
      }
      return new Date(this.baseDate.getTime());
    }

    resolveDaysCount() {
      if (this.config && typeof this.config.days === 'number' && this.config.days > 0) {
        return this.config.days;
      }
      if (this.config && typeof this.config.num_days === 'number' && this.config.num_days > 0) {
        return this.config.num_days;
      }
      return 7;
    }

    resolveCachedRange() {
      if (this.cachedRange && this.cachedRange.start && this.cachedRange.end) {
        return this.cachedRange;
      }
      if (this.options.cachedRange) {
        return {
          start: typeof this.options.cachedRange.start === 'string'
            ? this.options.cachedRange.start
            : this.formatDateISO(this.options.cachedRange.start),
          end: typeof this.options.cachedRange.end === 'string'
            ? this.options.cachedRange.end
            : this.formatDateISO(this.options.cachedRange.end),
        };
      }
      let start = this.options.cachedRangeStart ||
                  (this.data && (this.data.cached_range_start || this.data.range_start)) ||
                  (this.config && this.config.cached_range_start);
      let end = this.options.cachedRangeEnd ||
                (this.data && (this.data.cached_range_end || this.data.range_end)) ||
                (this.config && this.config.cached_range_end);

      if (!start && Array.isArray(this.data && this.data.days) && this.data.days.length > 0) {
        start = this.data.days[0].date;
        end = this.data.days[this.data.days.length - 1].date;
      }
      if (!start && this.data && this.data.date) {
        start = this.data.date;
        end = this.data.date;
      }

      if (start && end) {
        return { start: String(start), end: String(end) };
      }
      return null;
    }

    getCachedDateRange() {
      return this.resolveCachedRange();
    }

    init() {
      this.bindViewSwitcher();
      this.bindInteractions();
      this.updateViewUI(this.activeView);
      this.updateDateUI();
      this.fitDayEvents();
      this.decorateMealieEvents();
      this.applySpecialDayHighlights();
      this.applyEventFlairs();
      this.resetInactivityTimer();
    }

    fitDayEvents() {
      if (!this.element || typeof this.element.querySelectorAll !== 'function') return;
      const dayEvents = this.element.querySelectorAll('.cf-day-event');
      for (const evEl of dayEvents) {
        const timeEl = evEl.querySelector ? evEl.querySelector('.cf-event-time') : null;
        const titleEl = evEl.querySelector ? evEl.querySelector('.cf-event-title') : null;
        if (!timeEl || !titleEl) continue;

        if (evEl.classList && typeof evEl.classList.remove === 'function') {
          evEl.classList.remove('cf-event-compact');
        }

        const headerEl = evEl.querySelector ? evEl.querySelector('.cf-event-header') : null;
        let fits = true;

        if (typeof evEl.clientHeight === 'number' && evEl.clientHeight > 0) {
          const headerH = (headerEl && typeof headerEl.offsetHeight === 'number' && headerEl.offsetHeight > 0)
            ? headerEl.offsetHeight
            : (typeof timeEl.offsetHeight === 'number' ? timeEl.offsetHeight : 0);
          const titleH = typeof titleEl.offsetHeight === 'number' ? titleEl.offsetHeight : 0;
          if (headerH > 0 && titleH > 0) {
            if (headerH + titleH + 2 > evEl.clientHeight) {
              fits = false;
            }
          } else if (typeof evEl.scrollHeight === 'number' && evEl.scrollHeight > evEl.clientHeight + 1) {
            const locEl = evEl.querySelector ? evEl.querySelector('.cf-event-location') : null;
            if (!locEl) {
              fits = false;
            }
          }
        }

        if (fits && typeof evEl.clientWidth === 'number' && evEl.clientWidth > 0) {
          const timeW = typeof timeEl.offsetWidth === 'number' ? timeEl.offsetWidth : 0;
          const titleW = typeof titleEl.scrollWidth === 'number' ? titleEl.scrollWidth : 0;
          if (timeW > 0 && titleW > 0 && timeEl.offsetTop === titleEl.offsetTop) {
            if (timeW + titleW + 8 > evEl.clientWidth) {
              fits = false;
            }
          }
        }

        if (!fits && evEl.classList && typeof evEl.classList.add === 'function') {
          evEl.classList.add('cf-event-compact');
        }
      }
    }

    bindViewSwitcher() {
      for (const btn of this.viewButtons) {
        btn.addEventListener('click', this.clickHandler);
      }
    }

    bindInteractions() {
      this.element.addEventListener('click', this.clickHandler);
      this.element.addEventListener('touchstart', this.touchStartHandler, { passive: true });
      this.element.addEventListener('touchmove', this.touchMoveHandler, { passive: true });
      this.element.addEventListener('touchend', this.touchEndHandler, { passive: true });
      this.element.addEventListener('touchcancel', this.touchCancelHandler, { passive: true });

      const activityEvents = ['pointerdown', 'mousedown'];
      for (const evt of activityEvents) {
        this.element.addEventListener(evt, this.activityHandler, { passive: true });
      }
      if (typeof document !== 'undefined' && document.addEventListener) {
        document.addEventListener('keydown', this.keydownHandler);
      }
    }

    handleTouchCancel() {
      this.isSwiping = false;
      this.justSwiped = false;
    }

    handleUserActivity() {
      this.resetInactivityTimer();
    }

    resetInactivityTimer() {
      if (this.inactivityTimer) {
        clearTimeout(this.inactivityTimer);
      }
      this.inactivityTimer = setTimeout(() => {
        this.handleInactivityTimeout();
      }, this.inactivityTimeout);
      if (this.inactivityTimer && typeof this.inactivityTimer.unref === 'function') {
        this.inactivityTimer.unref();
      }
    }

    handleInactivityTimeout() {
      this.inactivityTimer = null;

      // 1. If detail modal is open, dismiss it
      if (this.activeModal) {
        this.closeModal();
      }

      // 2. Return active view to defaultView
      if (this.activeView !== this.defaultView) {
        this.switchView(this.defaultView);
      }

      // 3. Reset date window to current/base date
      if (this.pageOffset !== 0) {
        this.resetDate();
      }

      if (typeof this.options.onInactivityReset === 'function') {
        this.options.onInactivityReset(this);
      }
    }

    handleKeyDown(e) {
      if (e.key === 'Escape' && this.activeModal) {
        this.closeModal();
      }
    }

    handleTouchStart(e) {
      this.resetInactivityTimer();
      if (this.activeModal) return;

      const touch = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e;
      this.touchStartX = touch.clientX !== undefined ? touch.clientX : (touch.pageX || 0);
      this.touchStartY = touch.clientY !== undefined ? touch.clientY : (touch.pageY || 0);
      this.touchCurrentX = this.touchStartX;
      this.touchCurrentY = this.touchStartY;
      this.touchStartTime = Date.now();
      this.isSwiping = true;
    }

    handleTouchMove(e) {
      this.resetInactivityTimer();
      if (!this.isSwiping || this.activeModal) return;

      const touch = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e;
      this.touchCurrentX = touch.clientX !== undefined ? touch.clientX : (touch.pageX || 0);
      this.touchCurrentY = touch.clientY !== undefined ? touch.clientY : (touch.pageY || 0);
    }

    handleTouchEnd(e) {
      this.resetInactivityTimer();
      if (!this.isSwiping || this.activeModal) {
        this.isSwiping = false;
        return;
      }
      this.isSwiping = false;

      const touch = (e.changedTouches && e.changedTouches[0]) || (e.touches && e.touches[0]) || e;
      const touchEndX = touch.clientX !== undefined ? touch.clientX : (this.touchCurrentX || 0);
      const touchEndY = touch.clientY !== undefined ? touch.clientY : (this.touchCurrentY || 0);
      const deltaX = touchEndX - this.touchStartX;
      const deltaY = touchEndY - this.touchStartY;
      const duration = Math.max(Date.now() - this.touchStartTime, 1);
      const absDeltaX = Math.abs(deltaX);
      const absDeltaY = Math.abs(deltaY);
      const velocity = absDeltaX / duration;

      const isFlick = absDeltaX >= this.swipeDistanceThreshold && velocity >= this.swipeVelocityThreshold && absDeltaX > absDeltaY * 1.5;
      const isDrag = absDeltaX >= 50 && duration < 800 && absDeltaX > absDeltaY * 1.5;
      if (isFlick || isDrag) {
        this.justSwiped = true;
        setTimeout(() => { this.justSwiped = false; }, 120);

        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }

        if (deltaX < 0) {
          // Swipe Left -> next / forward
          this.pageNext();
        } else {
          // Swipe Right -> prev / backward
          this.pagePrev();
        }
      }
    }

    handleClick(e) {
      if (this.justSwiped) {
        this.justSwiped = false;
        return;
      }
      this.resetInactivityTimer();

      // Check navigation buttons & today badge
      const prevBtn = e.target && e.target.closest && e.target.closest('.cf-nav-btn.cf-prev-btn');
      if (prevBtn) {
        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }
        this.pagePrev();
        return;
      }

      const nextBtn = e.target && e.target.closest && e.target.closest('.cf-nav-btn.cf-next-btn');
      if (nextBtn) {
        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }
        this.pageNext();
        return;
      }

      const todayBadge = e.target && e.target.closest && e.target.closest('.cf-today-badge');
      if (todayBadge) {
        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }
        this.resetDate();
        return;
      }

      // 1. Check view switcher button click
      const btn = e.target && e.target.closest && e.target.closest('.cf-view-btn');
      if (btn && this.element.contains(btn)) {
        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }
        const targetView = (btn.dataset.view || '').toLowerCase();
        if (targetView && (targetView === 'week' || targetView === 'month')) {
          this.switchView(targetView);
        }
        return;
      }

      // 2. Check modal backdrop or close button click
      if (this.activeModal) {
        const closeBtn = e.target && e.target.closest && e.target.closest('.cf-modal-close');
        const overlay = e.target && e.target.classList && e.target.classList.contains('cf-modal-overlay');
        if (closeBtn || overlay) {
          this.closeModal();
          return;
        }
        const modalCard = e.target && e.target.closest && e.target.closest('.cf-modal-card');
        if (modalCard) {
          return;
        }
      }

      // 3. Check event element click/tap (ignore decorative collapsed gap markers)
      if (e.target && e.target.closest && e.target.closest('.cf-collapsed-gap, .cf-collapsed-gap-label')) {
        return;
      }
      const eventEl = e.target && e.target.closest && e.target.closest('.cf-event-clickable, .cf-event, .cf-day-event, .cf-edge-event, .cf-month-event-title, .cf-has-events, [data-event-id]');
      if (eventEl && this.element.contains(eventEl)) {
        const evData = this.resolveEventData(eventEl);
        if (evData) {
          if (typeof e.preventDefault === 'function') {
            e.preventDefault();
          }
          this.openModal(evData);
        }
      }
    }

    resolveEventData(eventEl) {
      if (!eventEl) return null;

      // 1. Search eventIndex by data-event-id
      const id = eventEl.dataset && eventEl.dataset.eventId;
      if (id && this.eventIndex.has(id)) {
        return this.eventIndex.get(id);
      }

      // 2. Search eventIndex by title
      const titleAttr = (eventEl.dataset && (eventEl.dataset.eventTitle || eventEl.dataset.title)) || (eventEl.getAttribute && eventEl.getAttribute('title'));
      if (titleAttr && this.eventIndex.has(titleAttr)) {
        return this.eventIndex.get(titleAttr);
      }

      // 3. Check Month view day cell with events
      if (eventEl.dataset && eventEl.dataset.date) {
        const daysList = Array.isArray(this.data.month_days) ? this.data.month_days : (Array.isArray(this.data.days) ? this.data.days : null);
        if (daysList) {
          const day = daysList.find(d => d.date === eventEl.dataset.date);
          if (day && Array.isArray(day.events) && day.events.length > 0) {
            return day.events[0];
          }
        }
      }

      // 4. Construct from DOM dataset attributes
      if (eventEl.dataset && (eventEl.dataset.eventTitle || eventEl.dataset.title || eventEl.textContent)) {
        const title = eventEl.dataset.eventTitle || eventEl.dataset.title || (eventEl.querySelector && eventEl.querySelector('.cf-event-title')?.textContent) || eventEl.textContent?.trim();
        const start = eventEl.dataset.eventStart || eventEl.dataset.start || (eventEl.querySelector && eventEl.querySelector('.cf-event-time')?.textContent) || '';
        const end = eventEl.dataset.eventEnd || eventEl.dataset.end || '';
        const allDay = eventEl.dataset.eventAllday === 'true' || eventEl.dataset.allDay === 'true' || (eventEl.classList && eventEl.classList.contains('cf-allday')) || false;
        const location = eventEl.dataset.eventLocation || eventEl.dataset.location || (eventEl.querySelector && eventEl.querySelector('.cf-event-location')?.textContent) || '';
        const imageUrl = eventEl.dataset.eventImageUrl || eventEl.dataset.imageUrl || '';
        const description = eventEl.dataset.eventDescription || eventEl.dataset.description || '';
        const color = eventEl.dataset.color || (eventEl.style && eventEl.style.backgroundColor) || 'var(--mm-accent-purple)';
        const pattern = eventEl.dataset.pattern || 'solid';

        return {
          id: id || 'ev-' + Math.random().toString(36).slice(2, 8),
          title: title || 'Untitled Event',
          start,
          end,
          all_day: allDay,
          location,
          image_url: imageUrl,
          description,
          color,
          pattern,
          owners: [],
          initials: [],
        };
      }

      return null;
    }

    openModal(eventData) {
      if (!eventData) return;

      // Close previous modal without resume
      if (this.activeModal) {
        this.closeModal(true);
      }

      const ev = typeof eventData === 'object' ? eventData : { title: String(eventData) };

      // 1. Overlay
      const overlay = document.createElement('div');
      overlay.className = 'cf-modal-overlay';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-label', ev.title || 'Event Details');

      // 2. Modal card
      const card = document.createElement('div');
      card.className = 'cf-modal-card';

      // Illustrated SVG flair banner (unless Mealie recipe hero photo)
      const isEventFlairEnabled = this.config.event_flair !== false &&
        (this.element ? this.element.dataset.eventFlair !== 'false' : true);
      const heroOrLoc = (ev.image_url && ev.image_url.trim()) || (ev.location && ev.location.trim()) || '';
      const isMealieHero = Boolean(heroOrLoc && (heroOrLoc.startsWith('/hearth-api/mealie/image/') || heroOrLoc.includes('/api/media/recipes/') || heroOrLoc.startsWith('data:image/')));
      if (isEventFlairEnabled && !isMealieHero) {
        const flair = detectEventFlair(ev.title);
        if (flair) {
          const bannerEl = document.createElement('div');
          bannerEl.className = `cf-modal-flair-banner cf-modal-flair-${flair}`;
          bannerEl.dataset.flair = flair;
          bannerEl.setAttribute('aria-hidden', 'true');
          bannerEl.innerHTML = getModalFlairBannerSvg(flair);
          card.appendChild(bannerEl);
        }
      }

      // 3. Header: Title + Close button
      const header = document.createElement('div');
      header.className = 'cf-modal-header';

      const titleEl = document.createElement('h3');
      titleEl.className = 'cf-modal-title';
      titleEl.textContent = ev.title || 'Untitled Event';
      header.appendChild(titleEl);

      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'cf-modal-close';
      closeBtn.setAttribute('aria-label', 'Close');
      closeBtn.innerHTML = '&times;';
      closeBtn.addEventListener('click', (e) => {
        if (typeof e.stopPropagation === 'function') {
          e.stopPropagation();
        }
        this.closeModal();
      });
      header.appendChild(closeBtn);
      card.appendChild(header);

      // 4. Formatted Time
      let timeText = '';
      if (ev.all_day || ev.allDay || ev.is_all_day) {
        timeText = 'All Day';
      } else if (ev.start && ev.end) {
        timeText = `${ev.start} – ${ev.end}`;
      } else if (ev.start) {
        timeText = ev.start;
      }
      if (timeText) {
        const timeEl = document.createElement('div');
        timeEl.className = 'cf-modal-time';
        timeEl.textContent = `🕒 ${timeText}`;
        card.appendChild(timeEl);
      }

      // 5. Location or Mealie Recipe Hero Image
      if (heroOrLoc) {
        if (isMealieHero) {
          const imgWrap = document.createElement('div');
          imgWrap.style.cssText = 'width:100%;height:190px;border-radius:12px;overflow:hidden;margin:8px 0 10px;background:rgba(15,23,42,0.4);';
          const imgEl = document.createElement('img');
          imgEl.src = heroOrLoc;
          imgEl.alt = ev.title || 'Meal Photo';
          imgEl.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
          imgWrap.appendChild(imgEl);
          card.appendChild(imgWrap);
        } else if (ev.location && ev.location.trim()) {
          const locEl = document.createElement('div');
          locEl.className = 'cf-modal-location';
          locEl.textContent = `📍 ${ev.location.trim()}`;
          card.appendChild(locEl);
        }
      }

      // 6. Owner chips / initials
      const owners = ev.owners || [];
      const initials = ev.initials || [];
      const colors = ev.colors || (ev.color ? [ev.color] : []);
      const patterns = ev.patterns || (ev.pattern ? [ev.pattern] : ['solid']);

      if (owners.length > 0 || initials.length > 0) {
        const ownersContainer = document.createElement('div');
        ownersContainer.className = 'cf-modal-owners';

        const count = Math.max(owners.length, initials.length);
        for (let i = 0; i < count; i++) {
          const ownerName = owners[i] || '';
          const initial = initials[i] || (ownerName ? ownerName.charAt(0).toUpperCase() : '');
          const color = colors[i] || ev.color || 'var(--mm-accent-purple)';
          const pattern = patterns[i] || ev.pattern || 'solid';

          const chip = document.createElement('span');
          chip.className = `cf-modal-owner-chip cf-pattern-${pattern}`;

          const badge = document.createElement('span');
          badge.className = 'cf-modal-owner-badge';
          badge.style.backgroundColor = color;
          badge.textContent = initial;
          chip.appendChild(badge);

          if (ownerName) {
            const nameSpan = document.createElement('span');
            nameSpan.className = 'cf-modal-owner-name';
            nameSpan.textContent = ownerName;
            chip.appendChild(nameSpan);
          }
          ownersContainer.appendChild(chip);
        }
        card.appendChild(ownersContainer);
      }

      // 7. Event Description (full scrollable recipe for Mealie, 280 chars for normal events)
      if (ev.description && ev.description.trim()) {
        let desc = ev.description.trim().replace(/\\n/g, '\n').replace(/\\,/g, ',').replace(/\\;/g, ';');
        const isMealie = isMealieHero || (ev.title && ev.title.includes('🍽️'));
        if (!isMealie && desc.length > 280) {
          desc = desc.slice(0, 277) + '...';
        }
        const descEl = document.createElement('div');
        descEl.className = 'cf-modal-description';
        if (isMealie) {
          descEl.style.cssText = 'white-space:pre-wrap;max-height:320px;overflow-y:auto;font-size:0.86rem;line-height:1.45;padding-right:6px;';
        }
        descEl.textContent = desc;
        card.appendChild(descEl);
      }

      overlay.appendChild(card);

      // Backdrop dismiss
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
          this.closeModal();
        }
      });

      this.element.appendChild(overlay);
      this.activeModal = overlay;

      // Pause carousel rotation
      this.pauseCarousel();

      if (typeof this.options.onModalOpen === 'function') {
        this.options.onModalOpen(ev, this);
      }
    }

    closeModal(isReplacing = false) {
      if (!this.activeModal) return;

      const modalEl = this.activeModal;
      this.activeModal = null;
      if (modalEl.parentNode) {
        modalEl.parentNode.removeChild(modalEl);
      }

      if (!isReplacing) {
        this.resumeCarousel();
      }

      if (typeof this.options.onModalClose === 'function') {
        this.options.onModalClose(this);
      }
    }

    pauseCarousel() {
      if (typeof window !== 'undefined') {
        if (window.MirrormereCarousel && typeof window.MirrormereCarousel.pause === 'function') {
          try { window.MirrormereCarousel.pause(); } catch (_) {}
        }
        if (window.carousel && typeof window.carousel.pause === 'function') {
          try { window.carousel.pause(); } catch (_) {}
        }
      }
    }

    resumeCarousel() {
      if (typeof window !== 'undefined') {
        if (window.MirrormereCarousel && typeof window.MirrormereCarousel.resume === 'function') {
          try { window.MirrormereCarousel.resume(); } catch (_) {}
        }
        if (window.carousel && typeof window.carousel.resume === 'function') {
          try { window.carousel.resume(); } catch (_) {}
        }
      }
    }

    switchView(targetView) {
      const view = (targetView || '').toLowerCase();
      if (view !== 'week' && view !== 'month') {
        return;
      }
      this.activeView = view;
      this.element.dataset.activeView = view;
      this.updateViewUI(view);

      if (typeof this.options.onViewChange === 'function') {
        this.options.onViewChange(view, this);
      }
    }

    page(direction) {
      if (direction === 'next' || direction === 'forward') {
        return this.pageNext();
      }
      if (direction === 'prev' || direction === 'backward') {
        return this.pagePrev();
      }
      return false;
    }

    pageNext() {
      const cached = this.resolveCachedRange();
      const prevDate = new Date(this.currentDate.getTime());
      const prevWeekStart = new Date(this.currentWeekStart.getTime());
      const prevOffset = this.pageOffset;

      const rollbackFn = () => {
        this.currentDate = prevDate;
        this.currentWeekStart = prevWeekStart;
        this.pageOffset = prevOffset;
        this.updateDateUI();
      };

      if (this.activeView === 'week') {
        const step = this.resolveDaysCount();
        const nextStart = new Date(this.currentWeekStart.getTime());
        nextStart.setDate(nextStart.getDate() + step);
        const nextStartStr = this.formatDateISO(nextStart);
        if (cached && cached.end && nextStartStr > cached.end) {
          this.showSyncIndicator('Not synced yet');
          return false;
        }
        this.currentWeekStart = nextStart;
        this.currentDate = new Date(nextStart.getTime());
        this.pageOffset += 1;
        this.hideSyncIndicator();
        this.updateDateUI();
        if (typeof this.options.onPageChange === 'function') {
          this.options.onPageChange('next', this.currentWeekStart, this);
        }
        this.loadDateView(this.currentWeekStart, this.activeView, rollbackFn);
        return true;
      }

      if (this.activeView === 'month') {
        const nextMonth = new Date(this.currentDate.getTime());
        nextMonth.setDate(1);
        nextMonth.setMonth(nextMonth.getMonth() + 1);
        const nextMonthStr = this.formatDateISO(nextMonth).slice(0, 7);
        if (cached && cached.end && nextMonthStr > cached.end.slice(0, 7)) {
          this.showSyncIndicator('Not synced yet');
          return false;
        }
        this.currentDate = nextMonth;
        this.pageOffset += 1;
        this.hideSyncIndicator();
        this.updateDateUI();
        if (typeof this.options.onPageChange === 'function') {
          this.options.onPageChange('next', this.currentDate, this);
        }
        this.loadDateView(this.currentDate, this.activeView, rollbackFn);
        return true;
      }

      return false;
    }

    pagePrev() {
      const cached = this.resolveCachedRange();
      const prevDate = new Date(this.currentDate.getTime());
      const prevWeekStart = new Date(this.currentWeekStart.getTime());
      const prevOffset = this.pageOffset;

      const rollbackFn = () => {
        this.currentDate = prevDate;
        this.currentWeekStart = prevWeekStart;
        this.pageOffset = prevOffset;
        this.updateDateUI();
      };

      if (this.activeView === 'week') {
        const step = this.resolveDaysCount();
        const prevStart = new Date(this.currentWeekStart.getTime());
        prevStart.setDate(prevStart.getDate() - step);
        const prevEnd = new Date(prevStart.getTime());
        prevEnd.setDate(prevEnd.getDate() + step - 1);
        const prevEndStr = this.formatDateISO(prevEnd);
        if (cached && cached.start && prevEndStr < cached.start) {
          this.showSyncIndicator('Not synced yet');
          return false;
        }
        this.currentWeekStart = prevStart;
        this.currentDate = new Date(prevStart.getTime());
        this.pageOffset -= 1;
        this.hideSyncIndicator();
        this.updateDateUI();
        if (typeof this.options.onPageChange === 'function') {
          this.options.onPageChange('prev', this.currentWeekStart, this);
        }
        this.loadDateView(this.currentWeekStart, this.activeView, rollbackFn);
        return true;
      }

      if (this.activeView === 'month') {
        const prevMonth = new Date(this.currentDate.getTime());
        prevMonth.setDate(1);
        prevMonth.setMonth(prevMonth.getMonth() - 1);
        const prevMonthStr = this.formatDateISO(prevMonth).slice(0, 7);
        if (cached && cached.start && prevMonthStr < cached.start.slice(0, 7)) {
          this.showSyncIndicator('Not synced yet');
          return false;
        }
        this.currentDate = prevMonth;
        this.pageOffset -= 1;
        this.hideSyncIndicator();
        this.updateDateUI();
        if (typeof this.options.onPageChange === 'function') {
          this.options.onPageChange('prev', this.currentDate, this);
        }
        this.loadDateView(this.currentDate, this.activeView, rollbackFn);
        return true;
      }

      return false;
    }

    resetDate() {
      this.currentDate = new Date(this.baseDate.getTime());
      this.currentWeekStart = new Date(this.baseWeekStart.getTime());
      this.pageOffset = 0;
      this.hideSyncIndicator();
      this.updateDateUI();
      this.loadDateView(this.baseDate, this.defaultView);
    }

    async loadDateView(targetDate, targetView, rollbackFn) {
      this.renderSeq++;
      const seq = this.renderSeq;

      if (this.abortController) {
        try {
          this.abortController.abort();
        } catch (_) {}
      }
      this.abortController = typeof AbortController !== 'undefined' ? new AbortController() : null;

      const dateStr = this.formatDateISO(targetDate);
      const viewStr = targetView || this.activeView;
      const cacheKey = `${dateStr}:${viewStr}`;

      if (this.renderCache.has(cacheKey)) {
        this.applyRenderedHTML(this.renderCache.get(cacheKey));
        return;
      }

      if (!this.fetchFn || !this.widgetId) {
        return;
      }

      try {
        let url = `api/widgets/${encodeURIComponent(this.widgetId)}/render?date=${encodeURIComponent(dateStr)}&view=${encodeURIComponent(viewStr)}`;
        try {
          if (typeof window !== 'undefined' && window.location && window.location.search) {
            const node = new URLSearchParams(window.location.search).get('node');
            if (node) {
              url += `&node=${encodeURIComponent(node)}`;
            }
          }
        } catch (_) {}
        const options = this.abortController ? { signal: this.abortController.signal } : {};
        const res = await this.fetchFn(url, options);

        if (seq !== this.renderSeq) return;

        if (!res || (typeof res.ok === 'boolean' && !res.ok)) {
          throw new Error(`Fetch failed with status ${res && res.status}`);
        }

        const html = typeof res.text === 'function' ? await res.text() : String(res);

        if (seq !== this.renderSeq) return;

        if (this.renderCache.size >= 20) {
          const oldestKey = this.renderCache.keys().next().value;
          this.renderCache.delete(oldestKey);
        }
        this.renderCache.set(cacheKey, html);

        this.applyRenderedHTML(html);
      } catch (err) {
        if ((err && err.name === 'AbortError') || seq !== this.renderSeq) {
          return;
        }
        this.showSyncIndicator('Sync failed');
        if (typeof rollbackFn === 'function') {
          rollbackFn();
        }
      } finally {
        if (seq === this.renderSeq) {
          this.abortController = null;
        }
      }
    }

    applyRenderedHTML(html) {
      let temp;
      if (typeof html === 'string') {
        const cleanHTML = html.replace(/<img\b([^>]*)\bonload=("[^"]*"|'[^']*'|[^\s>]*)/gi, '<img$1 data-disabled-onload=$2');
        if (typeof DOMParser !== 'undefined') {
          try {
            const parser = new DOMParser();
            const doc = parser.parseFromString(cleanHTML, 'text/html');
            temp = doc.body;
          } catch (_) {}
        }
        if (!temp && typeof document !== 'undefined' && document.createElement) {
          temp = document.createElement('div');
          temp.innerHTML = cleanHTML;
        }
        if (!temp) return;
      } else if (html && typeof html.querySelector === 'function') {
        temp = html;
      } else {
        return;
      }

      const views = ['week', 'month'];
      for (const v of views) {
        const newPanel = temp.querySelector(`.cf-view-panel.cf-view-${v}`) || temp.querySelector(`.cf-view-${v}`);
        const curPanel = this.viewPanels[v];
        if (newPanel && curPanel) {
          const display = curPanel.style.display;
          if (typeof curPanel.replaceWith === 'function') {
            curPanel.replaceWith(newPanel);
            newPanel.style.display = display;
            this.viewPanels[v] = newPanel;
          } else if (curPanel.parentNode && typeof curPanel.parentNode.replaceChild === 'function') {
            curPanel.parentNode.replaceChild(newPanel, curPanel);
            newPanel.style.display = display;
            this.viewPanels[v] = newPanel;
          } else {
            curPanel.innerHTML = newPanel.innerHTML;
          }
        }
      }

      for (const v of views) {
        const newLabel = temp.querySelector(`.cf-range-label.cf-label-${v}`);
        const curLabel = this.labels[v];
        if (newLabel && curLabel) {
          curLabel.textContent = newLabel.textContent;
        }
      }

      const newBadge = temp.querySelector('.cf-today-badge.cf-today-day') || temp.querySelector('.cf-today-badge');
      if (newBadge && this.todayBadge) {
        this.todayBadge.textContent = newBadge.textContent;
        if (newBadge.style && newBadge.style.display !== undefined) {
          this.todayBadge.style.display = newBadge.style.display;
        }
      }

      const newDataEl = temp.querySelector('.calendar-grid-data, .calendar-family-data');
      const curDataEl = this.element.querySelector('.calendar-grid-data, .calendar-family-data');
      if (newDataEl && curDataEl) {
        curDataEl.textContent = newDataEl.textContent;
      } else if (newDataEl && !curDataEl) {
        this.element.appendChild(newDataEl);
      }

      this.data = this.parseData();
      this.eventIndex = this.buildEventIndex();
      this.viewButtons = Array.from(this.element.querySelectorAll('.cf-view-btn'));

      if (this.data && this.data.cached_range_start && this.data.cached_range_end) {
        this.cachedRange = {
          start: String(this.data.cached_range_start),
          end: String(this.data.cached_range_end),
        };
      }

      this.element.dataset.currentDate = this.formatDateISO(this.currentDate);
      this.element.dataset.pageOffset = String(this.pageOffset);
      this.updateDateUI();
      this.fitDayEvents();
      this.decorateMealieEvents();
      this.applySpecialDayHighlights();
      this.applyEventFlairs();
    }

    showSyncIndicator(message = 'Not synced yet') {
      let indicator = this.element.querySelector('.cf-sync-indicator');
      if (!indicator) {
        indicator = document.createElement('div');
        indicator.className = 'cf-sync-indicator';
        indicator.setAttribute('role', 'status');
        indicator.setAttribute('aria-live', 'polite');
        const header = this.element.querySelector('.cf-header');
        if (header) {
          const heading = header.querySelector('.cf-date-heading') || header;
          heading.appendChild(indicator);
        } else {
          this.element.appendChild(indicator);
        }
      }
      indicator.textContent = message;
      indicator.style.display = '';

      if (this.syncIndicatorTimer) {
        clearTimeout(this.syncIndicatorTimer);
      }
      this.syncIndicatorTimer = setTimeout(() => {
        this.hideSyncIndicator();
      }, 2500);
      if (this.syncIndicatorTimer && typeof this.syncIndicatorTimer.unref === 'function') {
        this.syncIndicatorTimer.unref();
      }
    }

    hideSyncIndicator() {
      if (this.syncIndicatorTimer) {
        clearTimeout(this.syncIndicatorTimer);
        this.syncIndicatorTimer = null;
      }
      const indicator = this.element.querySelector('.cf-sync-indicator');
      if (indicator) {
        indicator.style.display = 'none';
      }
    }

    updateDateUI() {
      this.element.dataset.currentDate = this.formatDateISO(this.currentDate);
      this.element.dataset.pageOffset = String(this.pageOffset);

      if (this.activeView === 'week') {
        const label = this.labels.week || this.element.querySelector('.cf-range-label.cf-label-week') || this.element.querySelector('.cf-range-label');
        if (label) {
          const step = this.resolveDaysCount();
          const weekEnd = new Date(this.currentWeekStart.getTime());
          weekEnd.setDate(weekEnd.getDate() + step - 1);
          label.textContent = this.formatWeekLabel(this.currentWeekStart, weekEnd);
        }
      } else if (this.activeView === 'month') {
        const label = this.labels.month || this.element.querySelector('.cf-range-label.cf-label-month') || this.element.querySelector('.cf-range-label');
        if (label) {
          label.textContent = this.formatMonthLabel(this.currentDate);
        }
      }
      if (this.todayBadge) {
        this.todayBadge.style.display = (this.pageOffset !== 0) ? '' : 'none';
      }
    }

    formatDayLabel(d) {
      const weekdays = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      return `${weekdays[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}`;
    }

    formatWeekLabel(start, end) {
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      if (start.getFullYear() === end.getFullYear()) {
        if (start.getMonth() === end.getMonth()) {
          return `${months[start.getMonth()]} ${start.getDate()} – ${end.getDate()}, ${start.getFullYear()}`;
        }
        return `${months[start.getMonth()]} ${start.getDate()} – ${months[end.getMonth()]} ${end.getDate()}, ${start.getFullYear()}`;
      }
      return `${months[start.getMonth()]} ${start.getDate()}, ${start.getFullYear()} – ${months[end.getMonth()]} ${end.getDate()}, ${end.getFullYear()}`;
    }

    formatMonthLabel(d) {
      const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
      return `${months[d.getMonth()]} ${d.getFullYear()}`;
    }

    formatDateISO(d) {
      if (!d) return '';
      const year = d.getFullYear();
      const month = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    }

    parseDateParts(str) {
      if (!str) return null;
      if (str instanceof Date) {
        return { year: str.getFullYear(), month: str.getMonth() + 1, day: str.getDate() };
      }
      const parts = String(str).split('T')[0].split('-');
      if (parts.length >= 3) {
        const y = parseInt(parts[0], 10);
        const m = parseInt(parts[1], 10);
        const d = parseInt(parts[2], 10);
        if (!isNaN(y) && !isNaN(m) && !isNaN(d)) {
          return { year: y, month: m, day: d };
        }
      }
      return null;
    }

    createLocalDate(year, month, day) {
      return new Date(year, month - 1, day, 12, 0, 0);
    }

    updateViewUI(view) {
      // 1. Update root container view classes
      this.element.classList.remove('cf-view-day');
      this.element.classList.remove('cf-view-week');
      this.element.classList.remove('cf-view-month');
      this.element.classList.add(`cf-view-${view}`);

      // 2. Update view switcher buttons
      for (const btn of this.viewButtons) {
        const isCurrent = btn.dataset.view === view;
        if (isCurrent) {
          btn.classList.add('active');
          btn.setAttribute('aria-selected', 'true');
        } else {
          btn.classList.remove('active');
          btn.setAttribute('aria-selected', 'false');
        }
      }

      // 3. Update view panels if multiple panels exist in DOM
      for (const [key, panel] of Object.entries(this.viewPanels)) {
        if (panel && panel !== this.element) {
          panel.style.display = (key === view ? 'flex' : 'none');
        }
      }

      // 4. Update header date range labels if specific label elements exist
      for (const [key, label] of Object.entries(this.labels)) {
        if (label) {
          label.style.display = (key === view ? '' : 'none');
        }
      }

      // 5. Update TODAY badge visibility
      if (this.todayBadge) {
        this.todayBadge.style.display = (this.pageOffset !== 0) ? '' : 'none';
      }

      // 6. Refresh date labels for active view
      this.updateDateUI();
      if (view === 'week') {
        this.fitDayEvents();
        this.decorateMealieEvents();
      }
      this.applySpecialDayHighlights();
      this.applyEventFlairs();
    }

    destroy() {
      if (this.abortController) {
        try {
          this.abortController.abort();
        } catch (_) {}
        this.abortController = null;
      }
      this.renderCache.clear();
      if (this.inactivityTimer) {
        clearTimeout(this.inactivityTimer);
        this.inactivityTimer = null;
      }
      if (this.syncIndicatorTimer) {
        clearTimeout(this.syncIndicatorTimer);
        this.syncIndicatorTimer = null;
      }
      for (const btn of this.viewButtons) {
        btn.removeEventListener('click', this.clickHandler);
      }
      this.element.removeEventListener('click', this.clickHandler);
      this.element.removeEventListener('touchstart', this.touchStartHandler);
      this.element.removeEventListener('touchmove', this.touchMoveHandler);
      this.element.removeEventListener('touchend', this.touchEndHandler);
      this.element.removeEventListener('touchcancel', this.touchCancelHandler);
      const activityEvents = ['pointerdown', 'mousedown'];
      for (const evt of activityEvents) {
        this.element.removeEventListener(evt, this.activityHandler);
      }
      if (typeof document !== 'undefined' && document.removeEventListener) {
        document.removeEventListener('keydown', this.keydownHandler);
      }
      if (this.activeModal) {
        this.closeModal();
      }
    }
  }

  // Global controller registration
  const api = {
    instances: new Map(),
    mount(element, options = {}) {
      if (!element) return null;
      if (typeof element.isConnected === 'boolean' && !element.isConnected) {
        return null;
      }
      const widgetId = element.dataset.widgetId || element.dataset.widgetType || 'calendar-grid';
      const existing = this.instances.get(widgetId);
      if (existing) {
        if (existing.element === element) {
          return existing;
        }
        this.unmount(widgetId);
      }
      const instance = new CalendarFamilyInstance(element, options);
      this.instances.set(widgetId, instance);
      return instance;
    },
    unmount(widgetId) {
      const instance = this.instances.get(widgetId);
      if (instance) {
        instance.destroy();
        this.instances.delete(widgetId);
      }
    },
    getInstance(widgetId) {
      return this.instances.get(widgetId) || null;
    }
  };

  api.detectEventFlair = detectEventFlair;
  api.detectDaySpecialStatus = detectDaySpecialStatus;
  CalendarFamilyInstance.detectEventFlair = detectEventFlair;
  CalendarFamilyInstance.detectDaySpecialStatus = detectDaySpecialStatus;

  if (typeof window !== 'undefined') {
    window.MirrormereCalendarFamily = api;
    window.MirrormereCalendarGrid = api;
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { CalendarFamilyInstance, MirrormereCalendarFamily: api, MirrormereCalendarGrid: api, detectEventFlair, detectDaySpecialStatus };
  }
})();
