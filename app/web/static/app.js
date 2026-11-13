/* ==========================================================================
   AUA Hub — Client-Side Single-Page Application Logic
   ========================================================================== */

function getCachedUser() {
  try {
    const raw = localStorage.getItem("aua_hub_user");
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

const state = {
  user: getCachedUser(),
  token: localStorage.getItem("aua_hub_token") || null,
  activeView: "home",
  pendingRegEmail: "",
  // Have-Need state
  courses: [],
  semesterLabel: "Upcoming Semester",
  selectedHaveCourse: null,
  selectedNeedCourse: null,
  hnOnlyMine: false,
  hnPosts: [],
  // Feedbacks state
  fbType: "instructor",
  fbCatalogLoaded: false,
  fbInstructors: [],
  fbCourses: [],
  fbSelectedTarget: null,
  fbRating: 5,
  fbFilterTarget: null,
  fbItems: [],
  fbSummary: { total_reviews: 0, avg_rating: 0 },
  // Anonymous state
  anonSubtab: "posts",
  anonPostsPage: 1,
  anonPostsSearch: "",
  inboxSearch: "",
  syncCodeInfo: null,
  // Free Rooms state
  freeRoomsPayload: null,
  selectedBuilding: "all",
  roomAvailFilter: "all",
  // Telegram Groups state
  groupsPage: 1,
  // Campus Chat state
  unreadSpamCount: 0,
  chatPendingMediaUrl: "",
  chatPendingMediaType: "image",
  chatPendingMediaName: "",
  chatPendingImageUrl: "",
  chatReplyTo: null,
  chatEditingMsgId: null,
  activeMenuMsgId: null,
  pendingDeleteMsgId: null,
  chatMessagesMap: new Map(),
  chatIsScrolledUp: false,
  chatUnreadSinceScroll: 0,
  activeEmojiCategory: "smileys",
  emojiSearchQuery: "",
  ws: null,
  deferredPwaPrompt: null,
  // Public user cache for instant hover avatar preview & profile modal
  knownUsers: {},
  // Real-time In-App Notifications
  notifications: [],
  unreadNotificationCount: 0,
};

/* --------------------------------------------------------------------------
   API Helper & Toast Notifications
   -------------------------------------------------------------------------- */

function showToast(message, isError = false) {
  const stack = document.getElementById("toastStack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = "toast-item" + (isError ? " error" : "");
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 220);
  }, 3800);
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (!(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (state.token) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }
  const resp = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || `Request failed (${resp.status})`);
  }
  return data;
}

function trackWebEvent(eventType, metaInfo = "") {
  try {
    apiFetch("/api/analytics/event", {
      method: "POST",
      body: JSON.stringify({
        event_type: eventType,
        meta_info: typeof metaInfo === "string" ? metaInfo.slice(0, 256) : "",
      }),
    }).catch(() => { });
  } catch (e) { }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function encodeJsAttr(str) {
  return encodeURIComponent(String(str || "")).replace(/'/g, "%27");
}

function formatDateTime(isoStr) {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return isoStr;
  }
}

/* --------------------------------------------------------------------------
   Universal User Avatar Circle, Hover Image Preview & Public Profile Modal
   -------------------------------------------------------------------------- */

function registerKnownUser(u) {
  if (!u || !u.id) return null;
  const uid = Number(u.id);
  if (!uid) return null;
  const prev = state.knownUsers[uid] || {};
  const merged = {
    id: uid,
    name: u.name || u.author_name || prev.name || "AUA Student",
    avatar_url:
      u.avatar_url !== undefined
        ? u.avatar_url
        : u.author_avatar !== undefined
          ? u.author_avatar
          : prev.avatar_url || "",
    bio:
      u.bio !== undefined
        ? u.bio
        : u.author_bio !== undefined
          ? u.author_bio
          : prev.bio || "",
    inbox_link:
      u.inbox_link !== undefined
        ? u.inbox_link
        : u.author_inbox_link !== undefined
          ? u.author_inbox_link
          : prev.inbox_link || "",
    is_bot_synced:
      u.is_bot_synced !== undefined ? Boolean(u.is_bot_synced) : Boolean(prev.is_bot_synced),
    is_alumni:
      u.is_alumni !== undefined ? Boolean(u.is_alumni) : Boolean(prev.is_alumni),
    created_at: u.created_at || prev.created_at || null,
  };
  state.knownUsers[uid] = merged;
  return merged;
}

function getUserInitial(name) {
  return ((name || "A").trim().charAt(0) || "A").toUpperCase();
}

function renderAvatarCircleHtml(userObj, sizeClass = "user-avatar-sm", interactive = true) {
  const u = registerKnownUser(userObj) || userObj || {};
  const uid = Number(u.id) || 0;
  const isDeleted = Boolean(u.is_deleted || u.name === "Deleted Account");
  const name = isDeleted ? "Deleted Account" : (u.name || "AUA Student");
  const avatarUrl = isDeleted ? "/static/bot-logo.svg" : (u.avatar_url || "").trim();
  const initial = isDeleted ? "🎭" : getUserInitial(name);

  const shouldBeInteractive = interactive && uid && !isDeleted;
  const interactiveAttrs = shouldBeInteractive
    ? `onclick="event.stopPropagation(); openUserProfileModal(${uid})" onmouseenter="showAvatarHoverPreview(event, ${uid})" onmousemove="moveAvatarHoverPreview(event)" onmouseleave="hideAvatarHoverPreview()" title="View ${escapeHtml(name)}'s profile"`
    : (isDeleted ? `title="Deleted Account"` : "");
  const interactiveClass = shouldBeInteractive ? " avatar-interactive" : (isDeleted ? " avatar-deleted" : "");

  if (avatarUrl) {
    return `<span class="user-avatar-circle ${sizeClass}${interactiveClass}" ${interactiveAttrs}><img src="${escapeHtml(avatarUrl)}" alt="${escapeHtml(name)}" class="user-avatar-img" onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';" /><span class="user-avatar-fallback" style="display:none;">${escapeHtml(initial)}</span></span>`;
  }
  return `<span class="user-avatar-circle ${sizeClass}${interactiveClass}" ${interactiveAttrs}><span class="user-avatar-fallback">${escapeHtml(initial)}</span></span>`;
}

function renderClickableUserNameHtml(userObj, extraClass = "") {
  const u = registerKnownUser(userObj) || userObj || {};
  const uid = Number(u.id) || 0;
  const isDeleted = Boolean(u.is_deleted || u.name === "Deleted Account");
  const name = isDeleted ? "Deleted Account" : (u.name || "AUA Student");
  if (!uid || isDeleted) {
    return `<span class="${escapeClass(extraClass)}${isDeleted ? ' deleted-user-label' : ''}">${escapeHtml(name)}</span>`;
  }
  return `<button type="button" class="user-name-link ${escapeHtml(extraClass)}" onclick="event.stopPropagation(); openUserProfileModal(${uid})" onmouseenter="showAvatarHoverPreview(event, ${uid})" onmousemove="moveAvatarHoverPreview(event)" onmouseleave="hideAvatarHoverPreview()">${escapeHtml(name)}</button>`;
}

  function escapeClass(str) {
    return escapeHtml(str || "");
  }

  function showAvatarHoverPreview(ev, userId) {
    const popover = document.getElementById("avatarHoverPopover");
    if (!popover) return;
    const uid = Number(userId);
    const u = state.knownUsers[uid] || (state.user && Number(state.user.id) === uid ? state.user : null);
    if (!u) return;

    const initial = getUserInitial(u.name);
    const avatarUrl = (u.avatar_url || "").trim();

    popover.innerHTML = `
    <div class="avatar-hover-wrapper">
      <div class="avatar-hover-frame">
        ${avatarUrl
        ? `<img
                src="${escapeHtml(avatarUrl)}"
                alt="${escapeHtml(u.name)}"
                class="avatar-hover-img"
                onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
              />
              <div class="avatar-hover-fallback" style="display:none;">${escapeHtml(initial)}</div>`
        : `<div class="avatar-hover-fallback">${escapeHtml(initial)}</div>`
      }
      </div>
      <div class="avatar-hover-name-pill">
        <span class="avatar-hover-pill-dot"></span>
        <span class="avatar-hover-pill-name">${escapeHtml(u.name)}</span>
      </div>
    </div>
  `;

    popover.classList.add("visible");
    moveAvatarHoverPreview(ev);
  }

  function moveAvatarHoverPreview(ev) {
    const popover = document.getElementById("avatarHoverPopover");
    if (!popover || !popover.classList.contains("visible") || !ev) return;

    const pad = 16;
    const popW = 150;
    const popH = 150;
    let x = (ev.clientX || 0) + 16;
    let y = (ev.clientY || 0) - popH - 12;

    if (x + popW + pad > window.innerWidth) {
      x = Math.max(pad, (ev.clientX || 0) - popW - 16);
    }
    if (y < pad) {
      y = Math.min(window.innerHeight - popH - pad, (ev.clientY || 0) + 24);
    }

    popover.style.left = `${Math.round(x)}px`;
    popover.style.top = `${Math.round(y)}px`;
  }

  function hideAvatarHoverPreview() {
    const popover = document.getElementById("avatarHoverPopover");
    if (popover) {
      popover.classList.remove("visible");
    }
  }

  async function openUserProfileModal(userId) {
    hideAvatarHoverPreview();
    const uid = Number(userId);
    if (!uid) return;

    const modal = document.getElementById("userProfileModal");
    const body = document.getElementById("userProfileModalBody");
    if (!modal || !body) return;

    const cached =
      state.knownUsers[uid] ||
      (state.user && state.user.id === uid ? registerKnownUser(state.user) : null);
    if (cached && (cached.is_deleted || cached.name === "Deleted Account")) {
      showToast("This account has been deleted.", true);
      return;
    }

    modal.classList.add("open");

    if (cached) {
      renderUserProfileModalContent(cached, Boolean(state.user && state.user.id === uid), [], []);
    } else {
      body.innerHTML = `
      <div style="text-align:center; padding:44px 20px;">
        <div style="font-size:28px; margin-bottom:10px;">⏳</div>
        <div style="font-size:15px; font-weight:700; color:var(--text-ice);">Loading Student Profile...</div>
      </div>
    `;
    }

    try {
      const data = await apiFetch(`/api/users/${uid}/profile`);
      if (data && data.user) {
        const freshUser = registerKnownUser(data.user);
        renderUserProfileModalContent(
          freshUser,
          Boolean(data.is_self),
          data.have_need_posts || [],
          data.telegram_groups || []
        );
      }
    } catch (err) {
      if (!cached) {
        body.innerHTML = `
        <div style="text-align:center; padding:36px 20px; color:var(--danger);">
          ${escapeHtml(err.message || "Could not load user profile.")}
        </div>
      `;
      }
    }
  }

  function closeUserProfileModal() {
    closeProfileAvatarViewer();
    const modal = document.getElementById("userProfileModal");
    if (modal) modal.classList.remove("open");
  }

  function openProfileAvatarViewer() {
    const viewer = document.getElementById("profileAvatarModalViewer");
    const u = state.modalProfileUser;
    if (!viewer || !u) return;

    const avatarUrl = (u.avatar_url || "").trim();
    const name = u.name || "AUA Student";
    const initial = getUserInitial(name);

    const imgEl = document.getElementById("profileAvatarViewerImg");
    const fallbackEl = document.getElementById("profileAvatarViewerFallback");
    const nameEl = document.getElementById("profileAvatarViewerName");

    if (nameEl) nameEl.textContent = name;

    if (avatarUrl && imgEl) {
      imgEl.src = avatarUrl;
      imgEl.style.display = "block";
      if (fallbackEl) fallbackEl.style.display = "none";
    } else {
      if (imgEl) imgEl.style.display = "none";
      if (fallbackEl) {
        fallbackEl.textContent = initial;
        fallbackEl.style.display = "flex";
      }
    }

    viewer.classList.remove("hidden");
  }

  function closeProfileAvatarViewer() {
    const viewer = document.getElementById("profileAvatarModalViewer");
    if (viewer) viewer.classList.add("hidden");
  }

  async function copyUserInboxLink(url) {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      showToast("Anonymous inbox link copied! 📋");
    } catch {
      showToast("Could not copy link automatically.", true);
    }
  }

  function jumpToUserSwapCourse(courseCode) {
    closeUserProfileModal();
    switchView("have-need");
    const searchInput = document.getElementById("hnFeedSearch");
    if (searchInput && courseCode) {
      searchInput.value = courseCode;
      loadHaveNeedPosts();
    }
  }

  function renderUserProfileModalContent(u, isSelf, haveNeedPosts = [], telegramGroups = []) {
    const body = document.getElementById("userProfileModalBody");
    if (!body || !u) return;

    state.modalProfileUser = u;

    const roleBadge = u.is_alumni ? "🎓 AUA Alumni" : "🎓 AUA Student";
    const syncedBadge = u.is_bot_synced
      ? `<span class="profile-modal-badge synced-badge">⚡ Bot Synced</span>`
      : "";
    const joinedLabel = u.created_at
      ? `Joined ${new Date(u.created_at).toLocaleDateString("en-US", { month: "short", year: "numeric" })}`
      : "";

    const bioHtml = u.bio
      ? `<div class="profile-modal-bio">${escapeHtml(u.bio)}</div>`
      : `<div class="profile-modal-bio empty">No bio added yet.</div>`;

    const statsHtml = `
      <div class="profile-modal-stats-row">
        <div class="profile-stat-box">
          <div class="profile-stat-val">${haveNeedPosts.length}</div>
          <div class="profile-stat-lbl">Active Swaps</div>
        </div>
        <div class="profile-stat-box">
          <div class="profile-stat-val">${telegramGroups.length}</div>
          <div class="profile-stat-lbl">Shared Groups</div>
        </div>
        <div class="profile-stat-box">
          <div class="profile-stat-val" style="font-size:13px; color:${u.is_bot_synced ? "#34d399" : "var(--neon-blue-bright)"};">
            ${u.is_bot_synced ? "⚡ Synced" : "✅ Verified"}
          </div>
          <div class="profile-stat-lbl">Account Status</div>
        </div>
      </div>
    `;

    const actions = [];
    if (u.inbox_link) {
      actions.push(`
      <a
        href="${escapeHtml(u.inbox_link)}"
        target="_blank"
        rel="noopener noreferrer"
        class="btn-illuminated"
        style="flex:1.4; min-width:170px;"
      >
        ✉️ Anonymous Message
      </a>
      <button
        type="button"
        class="btn-glass"
        style="padding:10px 13px;"
        onclick="copyUserInboxLink('${escapeHtml(u.inbox_link)}')"
        title="Copy Anonymous Inbox Link"
      >
        📋
      </button>
    `);
    }
    if (isSelf) {
      actions.push(`
      <button
        type="button"
        class="btn-glass"
        style="flex:1; min-width:140px;"
        onclick="closeUserProfileModal(); switchView('profile');"
      >
        ✏️ Edit My Profile
      </button>
    `);
    }

    let activityHtml = "";
    if (haveNeedPosts.length > 0) {
      const swapsItems = haveNeedPosts
        .slice(0, 4)
        .map((p) => {
          const hc = p.have_course || {};
          const nc = p.need_course || {};
          const hCode = hc.course_code || "Course";
          const nCode = nc.course_code || "Course";
          const hSec = hc.section ? ` (${hc.section})` : "";
          const nSec = nc.section ? ` (${nc.section})` : "";
          return `
            <div
              class="profile-activity-item"
              onclick="jumpToUserSwapCourse('${escapeHtml(hCode)}')"
              role="button"
              tabindex="0"
              title="Click to view in Have-Need"
            >
              <div style="min-width:0;">
                <div class="profile-activity-swap-codes">
                  <span style="color:#34d399;">HAVE ${escapeHtml(hCode)}${escapeHtml(hSec)}</span>
                  <span style="color:var(--text-secondary); margin:0 4px;">↔</span>
                  <span style="color:var(--neon-blue-bright);">NEED ${escapeHtml(nCode)}${escapeHtml(nSec)}</span>
                </div>
                <div class="profile-activity-sub">
                  ${escapeHtml(hc.course_title || "")}
                </div>
              </div>
              <span style="font-size:12px; color:var(--neon-blue-bright); font-weight:700; flex-shrink:0;">View &rarr;</span>
            </div>
          `;
        })
        .join("");

      activityHtml += `
        <div class="profile-modal-section">
          <div class="profile-modal-section-title">
            <span>🔄 Active Course Swaps</span>
            <span class="profile-modal-section-count">${haveNeedPosts.length}</span>
          </div>
          <div class="profile-modal-activity-list">
            ${swapsItems}
          </div>
        </div>
      `;
    }

    if (telegramGroups.length > 0) {
      const groupItems = telegramGroups
        .slice(0, 4)
        .map((g) => {
          const initial = getUserInitial(g.name || "G");
          const thumb = g.image_url
            ? `<img src="${escapeHtml(g.image_url)}" alt="" style="width:32px; height:32px; border-radius:9px; object-fit:cover; flex-shrink:0; border:1px solid rgba(56,189,248,0.28);" />`
            : `<div style="width:32px; height:32px; border-radius:9px; background:linear-gradient(135deg,#0284c7,#2563eb); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:13px; color:#fff; flex-shrink:0;">${escapeHtml(initial)}</div>`;
          return `
            <a
              href="${escapeHtml(g.invite_link)}"
              target="_blank"
              rel="noopener noreferrer"
              class="profile-activity-item"
              style="text-decoration:none;"
            >
              <div style="display:flex; align-items:center; gap:10px; min-width:0;">
                ${thumb}
                <div style="min-width:0;">
                  <div style="font-size:13px; font-weight:800; color:var(--text-white); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    ${escapeHtml(g.name)}
                  </div>
                  <div class="profile-activity-sub">
                    ${escapeHtml(g.description || "Telegram community group")}
                  </div>
                </div>
              </div>
              <span style="font-size:12px; color:var(--neon-blue-bright); font-weight:700; flex-shrink:0;">Join ↗</span>
            </a>
          `;
        })
        .join("");

      activityHtml += `
        <div class="profile-modal-section">
          <div class="profile-modal-section-title">
            <span>✈️ Shared Telegram Groups</span>
            <span class="profile-modal-section-count">${telegramGroups.length}</span>
          </div>
          <div class="profile-modal-activity-list">
            ${groupItems}
          </div>
        </div>
      `;
    }

    body.innerHTML = `
    <div class="profile-modal-hero">
      <div class="profile-modal-banner"></div>
      <div
        class="profile-modal-avatar-holder"
        onclick="openProfileAvatarViewer()"
        title="Click to view larger photo"
        role="button"
        tabindex="0"
        onkeydown="if(event.key==='Enter'||event.key===' ') openProfileAvatarViewer()"
      >
        ${renderAvatarCircleHtml(u, "user-avatar-xl", false)}
        <span class="avatar-zoom-pill" title="Click to enlarge">🔍</span>
      </div>
      <h2 class="profile-modal-name">${escapeHtml(u.name)}</h2>
      <div class="profile-modal-badges">
        <span class="profile-modal-badge">${roleBadge}</span>
        ${syncedBadge}
        ${joinedLabel ? `<span class="profile-modal-joined">${escapeHtml(joinedLabel)}</span>` : ""}
      </div>
    </div>

    ${statsHtml}

    ${bioHtml}

    ${actions.length > 0
        ? `<div class="profile-modal-actions">${actions.join("")}</div>`
        : ""
      }

    ${activityHtml}
  `;
  }

  /* --------------------------------------------------------------------------
     Navigation & View Switching (with Full Browser History / Back-Button Routing)
     -------------------------------------------------------------------------- */

  const VALID_VIEWS = [
    "home",
    "have-need",
    "feedbacks",
    "anonymous",
    "free-rooms",
    "groups",
    "spam-chat",
    "chat",
    "profile",
  ];

  function replayHomeHeroAnimation() {
    const wrapper = document.getElementById("homeHeroWrapper");
    if (!wrapper) return;
    wrapper.classList.remove("hero-animate-active");
    void wrapper.offsetWidth;
    wrapper.classList.add("hero-animate-active");
  }

  function switchView(viewName, pushHistory = true) {
    if (viewName === "chat") {
      viewName = "spam-chat";
    }
    if (!VALID_VIEWS.includes(viewName)) {
      viewName = "home";
    }

    // Close modals when switching views
    closeUserProfileModal();
    closeAuthModal();
    closeGroupModal();
    if (typeof closeEmojiPicker === "function") closeEmojiPicker();
    if (typeof closeProfileMenu === "function") closeProfileMenu();

    state.activeView = viewName;
    let activeBtn = null;
    document.querySelectorAll(".nav-tab").forEach((btn) => {
      const isAct = btn.getAttribute("data-view") === viewName;
      btn.classList.toggle("active", isAct);
      if (isAct) activeBtn = btn;
    });
    if (activeBtn && typeof activeBtn.scrollIntoView === "function") {
      try {
        activeBtn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
      } catch { }
    }
    const profileBtn = document.getElementById("navbarProfileBtn") || document.querySelector(".user-pill");
    if (profileBtn) {
      profileBtn.classList.toggle("active", viewName === "profile");
    }
    document.querySelectorAll(".view-section").forEach((sec) => {
      sec.classList.toggle("active", sec.id === `view-${viewName}`);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });

    // Update browser history so Back button returns to previous view / Home instead of closing site
    if (pushHistory) {
      const targetHash = viewName === "home" ? "" : `#${viewName}`;
      const currentHash = window.location.hash || "";
      if (currentHash !== targetHash) {
        const targetUrl = targetHash || window.location.pathname;
        history.pushState({ view: viewName }, "", targetUrl);
      }
    }

    if (viewName === "home") {
      replayHomeHeroAnimation();
    } else if (viewName === "have-need") {
      trackWebEvent("click_have_need");
      loadHaveNeedPosts();
    } else if (viewName === "feedbacks") {
      trackWebEvent("view_feedbacks");
      initFeedbacksView();
    } else if (viewName === "anonymous") {
      trackWebEvent("click_anonymous");
      if (state.anonSubtab === "posts") {
        loadAnonymousPosts(state.anonPostsPage);
      } else {
        loadAnonymousInbox();
      }
    } else if (viewName === "free-rooms") {
      trackWebEvent("click_free_rooms");
      if (!state.freeRoomsPayload) {
        loadFreeRooms(false);
      } else {
        renderFreeRooms();
      }
    } else if (viewName === "groups") {
      trackWebEvent("click_groups");
      loadTelegramGroups(state.groupsPage);
    } else if (viewName === "spam-chat") {
      trackWebEvent("click_chat");
      loadSpamChatMessages(true);
    } else if (viewName === "profile") {
      renderProfileView();
    }
  }

  function initNavigationHistory() {
    const rawHash = (window.location.hash || "").replace(/^#/, "").trim();
    const initialView = VALID_VIEWS.includes(rawHash) ? rawHash : "home";

    if (initialView !== "home") {
      // If user arrived via direct link/bookmark with hash (e.g. auahub.am#have-need),
      // initialize 'home' as root history state first, then push the target view.
      // That way, pressing browser/mouse Back will navigate back to Home instead of exiting!
      history.replaceState({ view: "home" }, "", window.location.pathname);
      history.pushState({ view: initialView }, "", `#${initialView}`);
      switchView(initialView, false);
    } else {
      history.replaceState({ view: "home" }, "", window.location.pathname);
      switchView("home", false);
    }

    window.addEventListener("popstate", (e) => {
      // 1. Close open modals if any
      closeUserProfileModal();
      closeAuthModal();
      closeGroupModal();

      // 2. Close mobile panels if open
      document.querySelectorAll(".mobile-form-panel.mobile-open").forEach((p) => {
        p.classList.remove("mobile-open");
      });

      // 3. Determine destination view
      let target = (e.state && e.state.view) || null;
      if (!target) {
        const h = (window.location.hash || "").replace(/^#/, "").trim();
        target = VALID_VIEWS.includes(h) ? h : "home";
      }

      switchView(target, false);
    });
  }

  function toggleMobileFormPanel(panelId, btnId, closedLabel, openLabel) {
    const panel = document.getElementById(panelId);
    const btn = document.getElementById(btnId);
    if (!panel) return;
    const isOpen = panel.classList.toggle("mobile-open");
    if (btn) {
      btn.textContent = isOpen ? openLabel : closedLabel;
      btn.classList.toggle("btn-glass", isOpen);
      btn.classList.toggle("btn-illuminated", !isOpen);
    }
  }

  function openMobileFormPanel(panelId, btnId, openLabel) {
    const panel = document.getElementById(panelId);
    const btn = document.getElementById(btnId);
    if (!panel) return;
    panel.classList.add("mobile-open");
    if (btn) {
      btn.textContent = openLabel;
      btn.classList.add("btn-glass");
      btn.classList.remove("btn-illuminated");
    }
  }

  function closeMobileFormPanel(panelId, btnId, closedLabel) {
    const panel = document.getElementById(panelId);
    const btn = document.getElementById(btnId);
    if (!panel) return;
    panel.classList.remove("mobile-open");
    if (btn) {
      btn.textContent = closedLabel;
      btn.classList.remove("btn-glass");
      btn.classList.add("btn-illuminated");
    }
  }

  function updateUnreadBadge(count) {
    state.unreadSpamCount = Math.max(0, Number(count) || 0);
    const badge = document.getElementById("navUnreadBadge");
    if (!badge) return;
    if (state.unreadSpamCount > 0 && state.activeView !== "spam-chat") {
      badge.textContent = state.unreadSpamCount > 99 ? "99+" : String(state.unreadSpamCount);
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  /* --------------------------------------------------------------------------
     1. Authentication & OTP Registration
     -------------------------------------------------------------------------- */

  async function checkCurrentSession() {
    try {
      const data = await apiFetch("/api/auth/me");
      if (data.authenticated && data.user) {
        state.user = data.user;
        try { localStorage.setItem("aua_hub_user", JSON.stringify(data.user)); } catch { }
        updateUnreadBadge(data.unread_spam_count || 0);
        loadNotifications();
      } else {
        state.user = null;
        try { localStorage.removeItem("aua_hub_user"); } catch { }
      }
    } catch {
      if (!state.token) {
        state.user = null;
        try { localStorage.removeItem("aua_hub_user"); } catch { }
      }
    }
    renderNavbarAuth();
    const stream = document.getElementById("spamMessagesStream");
    if (stream && (state.activeView === "spam-chat" || state.activeView === "chat")) {
      loadSpamChatMessages(false);
    }
  }

  function renderNavbarAuth() {
    const container = document.getElementById("navbarAuthArea");
    if (!container) return;

    if (!state.user) {
      container.innerHTML = `
      <button class="btn-glass" onclick="openAuthModal('login')">Sign In</button>
      <button class="btn-illuminated" onclick="openAuthModal('register')">Register</button>
    `;
      return;
    }

    registerKnownUser(state.user);
    const avatarHtml = renderAvatarCircleHtml(state.user, "user-avatar-sm", false);
    const isProfileActive = state.activeView === "profile" ? " active" : "";
    const unreadCount = state.unreadNotificationCount || 0;
    const badgeCls = unreadCount > 0 ? "" : " hidden";
    const badgeTxt = unreadCount > 99 ? "99+" : String(unreadCount);

    container.innerHTML = `
    <button
      id="navbarNotifBtn"
      class="navbar-notif-btn"
      onclick="toggleNotificationPopover(event)"
      title="Notifications"
      aria-label="View notifications"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
      </svg>
      <span id="navbarNotifBadge" class="navbar-notif-badge${badgeCls}">
        ${badgeTxt}
      </span>
    </button>
    <button
      id="navbarProfileBtn"
      class="navbar-profile-btn${isProfileActive}"
      onclick="toggleProfileMenu(event)"
      title="${escapeHtml(state.user.name)} - Account Menu"
      aria-label="User account menu"
      aria-haspopup="true"
    >
      ${avatarHtml}
    </button>
  `;
  }

  function openAuthModal(tab = "login") {
    document.getElementById("authModal").classList.add("open");
    switchAuthTab(tab);
  }

  function closeAuthModal() {
    document.getElementById("authModal").classList.remove("open");
  }

  function switchAuthTab(tab) {
    document.getElementById("authTabLogin").classList.toggle("active", tab === "login");
    document.getElementById("authTabRegister").classList.toggle("active", tab === "register" || tab === "otp");
    document.getElementById("loginForm").style.display = tab === "login" ? "block" : "none";
    document.getElementById("registerForm").style.display = tab === "register" ? "block" : "none";
    document.getElementById("otpForm").style.display = tab === "otp" ? "block" : "none";
  }

  async function submitLogin(e) {
    e.preventDefault();
    const email = document.getElementById("loginEmail").value.trim();
    const password = document.getElementById("loginPassword").value;
    try {
      const res = await apiFetch("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      state.token = res.token;
      localStorage.setItem("aua_hub_token", res.token);
      state.user = res.user;
      try { localStorage.setItem("aua_hub_user", JSON.stringify(res.user)); } catch { }
      try {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send(JSON.stringify({ type: "auth", token: res.token }));
        }
      } catch { }
      updateUnreadBadge(res.unread_spam_count || 0);
      loadNotifications();
      renderNavbarAuth();
      closeAuthModal();
      showToast(`Welcome back, ${res.user.name}!`);
      refreshActiveViewData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function submitRegister(e) {
    e.preventDefault();
    const name = document.getElementById("regName").value.trim();
    const email = document.getElementById("regEmail").value.trim();
    const password = document.getElementById("regPassword").value;
    const confirm_password = document.getElementById("regConfirmPassword").value;

    const btn = document.getElementById("regSubmitBtn");
    btn.disabled = true;
    btn.textContent = "Sending OTP...";

    try {
      const res = await apiFetch("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ name, email, password, confirm_password }),
      });
      state.pendingRegEmail = res.email;
      const subtext = `We sent a 6-digit verification code to ${res.email}.`;
      document.getElementById("otpSentSubtext").textContent = subtext;
      switchAuthTab("otp");
      showToast(res.message || "OTP sent to your AUA email!");
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Send 6-Digit Verification OTP 📧";
    }
  }

  async function submitVerifyOtp(e) {
    e.preventDefault();
    const code = document.getElementById("otpCodeInput").value.trim();
    try {
      const res = await apiFetch("/api/auth/verify-otp", {
        method: "POST",
        body: JSON.stringify({ email: state.pendingRegEmail, code }),
      });
      state.token = res.token;
      localStorage.setItem("aua_hub_token", res.token);
      state.user = res.user;
      try { localStorage.setItem("aua_hub_user", JSON.stringify(res.user)); } catch { }
      try {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send(JSON.stringify({ type: "auth", token: res.token }));
        }
      } catch { }
      updateUnreadBadge(res.unread_spam_count || 0);
      loadNotifications();
      renderNavbarAuth();
      closeAuthModal();
      showToast("Account verified and signed in! Welcome to AUA Hub ✨");
      refreshActiveViewData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function logoutUser() {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch { }
    state.token = null;
    state.user = null;
    try {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "auth", token: "" }));
      }
    } catch { }
    state.notifications = [];
    state.unreadNotificationCount = 0;
    closeNotificationPopover();
    closeProfileMenu();
    localStorage.removeItem("aua_hub_token");
    try { localStorage.removeItem("aua_hub_user"); } catch { }
    updateUnreadBadge(0);
    renderNavbarAuth();
    refreshActiveViewData();
    showToast("Signed out of AUA Hub.");
  }

  function refreshActiveViewData() {
    switchView(state.activeView);
  }

  /* --------------------------------------------------------------------------
     1b. In-App Real-Time Notifications
     -------------------------------------------------------------------------- */

  async function loadNotifications() {
    if (!state.user) return;
    try {
      const res = await apiFetch("/api/notifications");
      if (res && res.ok) {
        state.notifications = res.notifications || [];
        state.unreadNotificationCount = res.unread_count || 0;
        updateNotificationBadge();
        const popover = document.getElementById("notificationsPopover");
        if (popover && !popover.classList.contains("hidden") && popover.style.display !== "none") {
          renderNotificationsList();
        }
      }
    } catch (err) {
      // Non-fatal if temporary network glitch
    }
  }

  function updateNotificationBadge() {
    const badge = document.getElementById("navbarNotifBadge");
    const unreadPill = document.getElementById("notifUnreadBadgePill");
    const count = state.unreadNotificationCount || 0;

    if (badge) {
      if (count > 0) {
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }

    if (unreadPill) {
      if (count > 0) {
        unreadPill.textContent = `${count} new`;
        unreadPill.classList.remove("hidden");
      } else {
        unreadPill.classList.add("hidden");
      }
    }
  }

  function renderNotificationsList() {
    const listEl = document.getElementById("notificationsList");
    if (!listEl) return;

    if (!state.notifications || state.notifications.length === 0) {
      listEl.innerHTML = `<div class="notif-empty-state">No notifications yet</div>`;
      return;
    }

    listEl.innerHTML = state.notifications
      .map((n) => {
        let iconClass = "chat-reply";
        let iconContent = "💬";

        if (n.category === "chat_reply") {
          iconClass = "chat-reply";
          iconContent = "💬";
        } else if (n.category === "chat_reaction") {
          iconClass = "chat-reply";
          iconContent = (n.extra_data && n.extra_data.emoji) ? escapeHtml(n.extra_data.emoji) : "❤️";
        } else if (n.category === "have_need_match") {
          iconClass = "have-need-match";
          iconContent = "🔄";
        } else if (n.category === "inbox_message") {
          iconClass = "inbox-message";
          iconContent = "📬";
        }

        const unreadClass = n.is_read ? "" : " unread";
        const timeStr = typeof formatRelativeTime === "function" ? formatRelativeTime(n.created_at) : "";

        return `
        <div class="notif-item${unreadClass}" onclick="handleNotificationClick(${n.id})">
          <div class="notif-item-icon-wrap ${iconClass}">
            ${iconContent}
          </div>
          <div class="notif-item-body">
            <div class="notif-item-title">${escapeHtml(n.title)}</div>
            <div class="notif-item-content">${escapeHtml(n.content)}</div>
            <div class="notif-item-time">${escapeHtml(timeStr)}</div>
          </div>
        </div>
      `;
      })
      .join("");
  }

  function toggleNotificationPopover(e) {
    if (e) e.stopPropagation();
    const popover = document.getElementById("notificationsPopover");
    const backdrop = document.getElementById("notificationsBackdrop");
    if (!popover) return;

    const isOpen = !popover.classList.contains("hidden") && popover.style.display !== "none";
    if (isOpen) {
      closeNotificationPopover();
    } else {
      // Dismiss open hover preview or menus
      if (typeof hideAvatarHoverPreview === "function") hideAvatarHoverPreview();
      if (typeof closeTelegramMessageMenu === "function") closeTelegramMessageMenu();
      if (typeof closeProfileMenu === "function") closeProfileMenu();

      popover.style.display = "flex";
      popover.classList.remove("hidden");
      if (backdrop) {
        backdrop.style.display = "block";
        backdrop.classList.remove("hidden");
      }

      // Position desktop popover relative to bell button
      const btn = document.getElementById("navbarNotifBtn");
      if (btn && window.innerWidth > 768) {
        const rect = btn.getBoundingClientRect();
        popover.style.top = `${rect.bottom + 8}px`;
        popover.style.right = `${Math.max(12, window.innerWidth - rect.right)}px`;
        popover.style.left = "auto";
      }

      renderNotificationsList();
      loadNotifications();
    }
  }

  function closeNotificationPopover() {
    const popover = document.getElementById("notificationsPopover");
    const backdrop = document.getElementById("notificationsBackdrop");
    if (popover) {
      popover.classList.add("hidden");
      popover.style.display = "none";
    }
    if (backdrop) {
      backdrop.classList.add("hidden");
      backdrop.style.display = "none";
    }
  }

  async function markAllNotificationsAsRead() {
    if (!state.user || state.unreadNotificationCount === 0) return;
    try {
      const res = await apiFetch("/api/notifications/read-all", { method: "POST" });
      if (res && res.ok) {
        state.notifications.forEach((n) => { n.is_read = true; });
        state.unreadNotificationCount = 0;
        updateNotificationBadge();
        renderNotificationsList();
      }
    } catch (err) {
      showToast(err.message || "Failed to mark all as read", true);
    }
  }

  async function handleNotificationClick(notifId) {
    const notif = (state.notifications || []).find((n) => Number(n.id) === Number(notifId));
    if (!notif) return;

    // Mark as read locally immediately for responsiveness
    if (!notif.is_read) {
      notif.is_read = true;
      state.unreadNotificationCount = Math.max(0, state.unreadNotificationCount - 1);
      updateNotificationBadge();
      renderNotificationsList();
      apiFetch(`/api/notifications/${notifId}/read`, { method: "POST" }).catch(() => { });
    }

    closeNotificationPopover();

    // Deep-Link Navigation
    if (notif.target_type === "chat_message") {
      switchView("spam-chat");
      setTimeout(() => {
        if (notif.target_id) {
          scrollToMessage(notif.target_id);
        }
      }, 280);
    } else if (notif.target_type === "have_need") {
      switchView("have-need");
      const code = notif.extra_data?.have_course_code || notif.extra_data?.need_course_code;
      if (code) {
        const searchInput = document.getElementById("hnFeedSearch");
        if (searchInput) {
          searchInput.value = code;
          loadHaveNeedPosts();
        }
      }
    } else if (notif.target_type === "inbox") {
      switchView("anonymous");
      switchAnonymousSubtab("inbox");
    }
  }

  /* --------------------------------------------------------------------------
     1c. User Profile Dropdown Menu & Popover
     -------------------------------------------------------------------------- */

  function renderProfileMenu() {
    const popover = document.getElementById("profileMenuPopover");
    if (!popover || !state.user) return;

    const u = state.user;
    registerKnownUser(u);
    const avatarHtml = renderAvatarCircleHtml(u, "user-avatar-md", false);
    const isAlumni = Boolean(u.email && u.email.endsWith("@alumni.aua.am"));
    const roleBadge = isAlumni
      ? `<span class="badge-alumni" style="font-size:10.5px; padding:2px 7px;">Alumni</span>`
      : `<span class="badge-bot-synced" style="font-size:10.5px; padding:2px 7px;">Student</span>`;

    popover.innerHTML = `
    <div class="profile-menu-header" onclick="closeProfileMenu(); switchView('profile');" title="Open Full Profile">
      ${avatarHtml}
      <div class="profile-menu-user-info">
        <div class="profile-menu-name-row">
          <span class="profile-menu-user-name">${escapeHtml(u.name)}</span>
          ${roleBadge}
        </div>
        <div class="profile-menu-user-email">${escapeHtml(u.email)}</div>
      </div>
    </div>

    <div class="profile-menu-divider"></div>

    <div class="profile-menu-items">
      <button type="button" class="profile-menu-item" onclick="closeProfileMenu(); switchView('profile');">
        <span class="profile-menu-icon">👤</span>
        <div class="profile-menu-item-text">
          <div class="profile-menu-item-title">My Profile</div>
          <div class="profile-menu-item-desc">Customize picture, bio &amp; info</div>
        </div>
      </button>

      <button type="button" class="profile-menu-item" onclick="closeProfileMenu(); switchView('anonymous'); switchAnonymousSubtab('inbox');">
        <span class="profile-menu-icon">📬</span>
        <div class="profile-menu-item-text">
          <div class="profile-menu-item-title">Anonymous Inbox</div>
          <div class="profile-menu-item-desc">Synced Telegram replies</div>
        </div>
      </button>

      <button type="button" class="profile-menu-item" onclick="closeProfileMenu(); switchView('have-need'); setHaveNeedFilter(true);">
        <span class="profile-menu-icon">🔄</span>
        <div class="profile-menu-item-text">
          <div class="profile-menu-item-title">My Course Swaps</div>
          <div class="profile-menu-item-desc">Manage your Have-Need listings</div>
        </div>
      </button>
    </div>

    <div class="profile-menu-divider"></div>

    <div class="profile-menu-footer">
      <button type="button" class="profile-menu-item profile-menu-logout" onclick="closeProfileMenu(); logoutUser();">
        <span class="profile-menu-icon">🚪</span>
        <div class="profile-menu-item-text">
          <div class="profile-menu-item-title">Sign Out</div>
        </div>
      </button>
      <button type="button" class="profile-menu-item profile-menu-delete-account" onclick="closeProfileMenu(); promptDeleteAccount();">
        <span class="profile-menu-icon">🗑️</span>
        <div class="profile-menu-item-text">
          <div class="profile-menu-item-title">Delete Account</div>
        </div>
      </button>
    </div>
  `;
  }

  function toggleProfileMenu(e) {
    if (e) e.stopPropagation();
    const popover = document.getElementById("profileMenuPopover");
    const backdrop = document.getElementById("profileMenuBackdrop");
    if (!popover) return;

    const isOpen = !popover.classList.contains("hidden") && popover.style.display !== "none";
    if (isOpen) {
      closeProfileMenu();
    } else {
      // Dismiss other open menus
      if (typeof hideAvatarHoverPreview === "function") hideAvatarHoverPreview();
      if (typeof closeTelegramMessageMenu === "function") closeTelegramMessageMenu();
      if (typeof closeNotificationPopover === "function") closeNotificationPopover();

      renderProfileMenu();

      popover.style.display = "flex";
      popover.classList.remove("hidden");
      if (backdrop) {
        backdrop.style.display = "block";
        backdrop.classList.remove("hidden");
      }

      const btn = document.getElementById("navbarProfileBtn");
      if (btn) btn.classList.add("active");

      // Position desktop popover relative to profile button
      if (btn && window.innerWidth > 768) {
        const rect = btn.getBoundingClientRect();
        popover.style.top = `${rect.bottom + 8}px`;
        popover.style.right = `${Math.max(12, window.innerWidth - rect.right)}px`;
        popover.style.left = "auto";
      }
    }
  }

  function closeProfileMenu() {
    const popover = document.getElementById("profileMenuPopover");
    const backdrop = document.getElementById("profileMenuBackdrop");
    const btn = document.getElementById("navbarProfileBtn");
    if (btn) btn.classList.remove("active");
    if (popover) {
      popover.classList.add("hidden");
      popover.style.display = "none";
    }
    if (backdrop) {
      backdrop.classList.add("hidden");
      backdrop.style.display = "none";
    }
  }

  function promptDeleteAccount() {
    closeProfileMenu();
    const modal = document.getElementById("deleteAccountModal");
    if (modal) {
      modal.classList.remove("hidden");
      modal.style.display = "flex";
    }
  }

  function closeDeleteAccountModal() {
    const modal = document.getElementById("deleteAccountModal");
    if (modal) {
      modal.classList.add("hidden");
      modal.style.display = "none";
    }
  }

  async function confirmDeleteAccount() {
    const btn = document.getElementById("confirmDeleteAccountBtn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Deleting Account...";
    }

    try {
      const res = await apiFetch("/api/profile/delete-account", {
        method: "DELETE",
      });

      closeDeleteAccountModal();
      localStorage.removeItem("aua_hub_token");
      localStorage.removeItem("aua_hub_user");
      state.user = null;
      state.unreadNotificationCount = 0;
      renderNavbarAuth();
      showToast("Your account, anonymous inbox, and all account data have been deleted.", false);
      switchView("home");
    } catch (err) {
      showToast(err.message || "Failed to delete account.", true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Yes, Delete Account";
      }
    }
  }

  /* --------------------------------------------------------------------------
     2. <Have-Need> Course Section Exchange
     -------------------------------------------------------------------------- */

  async function loadCoursesCatalog(refresh = false) {
    try {
      const url = refresh ? "/api/courses?refresh=1&limit=700" : "/api/courses?limit=700";
      const data = await apiFetch(url);
      state.courses = data.courses || [];
      state.semesterLabel = data.semester || "Upcoming Semester";
      const pill = document.getElementById("activeSemesterPill");
      if (pill) {
        pill.textContent = `${state.semesterLabel} (${state.courses.length} sections)`;
      }
    } catch (err) {
      showToast("Could not load courses catalog: " + err.message, true);
    }
  }

  async function refreshCoursesFromSonis() {
    showToast("Syncing latest course sections from Jenzabar Sonis...");
    await loadCoursesCatalog(true);
    showToast(`Synced ${state.courses.length} sections for ${state.semesterLabel}!`);
  }

  const DAY_MAP_SHORT = {
    MON: "M", MONDAY: "M",
    TUE: "T", TUES: "T", TUESDAY: "T",
    WED: "W", WEDNESDAY: "W",
    THU: "TH", THUR: "TH", THURS: "TH", THURSDAY: "TH",
    FRI: "F", FRIDAY: "F",
    SAT: "SAT", SATURDAY: "SAT",
    SUN: "SUN", SUNDAY: "SUN",
  };
  const DAY_ORDER_LIST = ["M", "T", "W", "TH", "F", "SAT", "SUN"];
  const SINGLE_DAY_LABEL = { M: "MON", T: "TUE", W: "WED", TH: "THU", F: "FRI", SAT: "SAT", SUN: "SUN" };

  function formatScheduleTimes(raw) {
    if (!raw || typeof raw !== "string" || !raw.trim() || raw.trim().toUpperCase() === "TBA") {
      return "TBA";
    }
    let text = raw.trim();
    text = text.replace(/TUE\/THU\b/gi, "TTH");
    text = text.replace(/MON\/WED\/FRI\b/gi, "MWF");
    text = text.replace(/MON\/WED\b/gi, "MW");
    if (text.includes("MWF") || text.includes("TTH")) {
      return text;
    }

    const parts = text.split(",").map((p) => p.trim()).filter(Boolean);
    const parsed = [];
    for (const p of parts) {
      const match = p.match(/^([A-Za-z/]+)\s+(.+)$/);
      if (match) {
        const dayStr = match[1].toUpperCase();
        const timeStr = match[2].trim();
        const rawDays = dayStr.split(/[/,\s]+/).map((d) => d.trim()).filter(Boolean);
        const days = [];
        for (const d of rawDays) {
          if (DAY_MAP_SHORT[d]) {
            days.push(DAY_MAP_SHORT[d]);
          } else if (d) {
            days.push(d);
          }
        }
        parsed.push({ days, timeStr });
      } else {
        return text;
      }
    }

    const timeGroups = new Map();
    for (const { days, timeStr } of parsed) {
      const tNorm = timeStr.replace(/\s*-\s*/g, "-");
      if (!timeGroups.has(tNorm)) {
        timeGroups.set(tNorm, []);
      }
      const currentList = timeGroups.get(tNorm);
      for (const d of days) {
        if (!currentList.includes(d)) {
          currentList.push(d);
        }
      }
    }

    const formattedGroups = [];
    for (const [tNorm, dList] of timeGroups.entries()) {
      dList.sort((a, b) => {
        const idxA = DAY_ORDER_LIST.indexOf(a);
        const idxB = DAY_ORDER_LIST.indexOf(b);
        return (idxA !== -1 ? idxA : 99) - (idxB !== -1 ? idxB : 99);
      });

      let dayCode = "";
      const key = dList.join(",");
      if (key === "M,W,F") {
        dayCode = "MWF";
      } else if (key === "T,TH") {
        dayCode = "TTH";
      } else if (key === "M,W") {
        dayCode = "MW";
      } else if (key === "M,F") {
        dayCode = "MF";
      } else if (key === "W,F") {
        dayCode = "WF";
      } else if (key === "M,T,W,TH,F") {
        dayCode = "MTWTHF";
      } else if (dList.length === 1) {
        dayCode = SINGLE_DAY_LABEL[dList[0]] || dList[0];
      } else {
        dayCode = dList.map((d) => SINGLE_DAY_LABEL[d] || d).join(", ");
      }
      formattedGroups.push(`${dayCode} ${tNorm}`);
    }

    return formattedGroups.join(", ");
  }

  function openCourseDropdown(kind) {
    const inputVal = document.getElementById(`${kind}CourseSearch`).value;
    filterCourseDropdown(kind, inputVal);
  }

  function filterCourseDropdown(kind, query) {
    const dropdown = document.getElementById(`${kind}CourseDropdown`);
    if (!dropdown) return;

    const q = (query || "").trim().toLowerCase();
    const matches = state.courses
      .filter((c) => {
        if (!q) return true;
        const hay = `${c.course_code} ${c.course_title} ${c.section} ${c.instructor} ${c.schedule_times}`.toLowerCase();
        return hay.includes(q);
      })
      .slice(0, 40);

    if (matches.length === 0) {
      dropdown.innerHTML = `<div style="padding:12px; color:var(--text-secondary); font-size:13px;">No matching course sections found.</div>`;
    } else {
      dropdown.innerHTML = matches
        .map(
          (c) => `
        <div class="course-option" onclick="selectCourseOption('${kind}', ${c.id})">
          <div class="course-option-title">
            <span style="color:var(--neon-blue-bright);">${escapeHtml(c.course_code)}</span>
            <span style="opacity:0.85;">[Sec ${escapeHtml(c.section)}]</span> — ${escapeHtml(c.course_title)}
          </div>
          <div class="course-option-meta">
            ${escapeHtml(c.instructor)} &bull; 🕒 ${escapeHtml(formatScheduleTimes(c.schedule_times))}
          </div>
        </div>
      `
        )
        .join("");
    }
    dropdown.classList.add("open");
  }

  function selectCourseOption(kind, courseId) {
    const course = state.courses.find((c) => c.id === courseId);
    if (!course) return;
    if (kind === "have") {
      state.selectedHaveCourse = course;
    } else {
      state.selectedNeedCourse = course;
    }
    const dropdown = document.getElementById(`${kind}CourseDropdown`);
    if (dropdown) dropdown.classList.remove("open");
    document.getElementById(`${kind}CourseSearch`).value = "";
    renderSelectedCourseChip(kind);
  }

  function clearSelectedCourse(kind) {
    if (kind === "have") {
      state.selectedHaveCourse = null;
    } else {
      state.selectedNeedCourse = null;
    }
    renderSelectedCourseChip(kind);
  }

  function renderSelectedCourseChip(kind) {
    const container = document.getElementById(`${kind}SelectedContainer`);
    if (!container) return;
    const c = kind === "have" ? state.selectedHaveCourse : state.selectedNeedCourse;
    if (!c) {
      container.innerHTML = "";
      return;
    }
    container.innerHTML = `
    <div class="selected-course-chip">
      <div>
        <div style="font-size:13.5px; font-weight:800; color:var(--text-white);">
          <span style="color:var(--neon-blue-bright);">${escapeHtml(c.course_code)}</span> (Sec ${escapeHtml(c.section)}) — ${escapeHtml(c.course_title)}
        </div>
        <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">
          ${escapeHtml(c.instructor)} &bull; 🕒 ${escapeHtml(formatScheduleTimes(c.schedule_times))}
        </div>
      </div>
      <button type="button" class="btn-glass" style="padding:3px 9px; font-size:12px;" onclick="clearSelectedCourse('${kind}')">&times;</button>
    </div>
  `;
  }

  // Close course dropdowns when clicking outside
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".course-picker-wrapper")) {
      document.querySelectorAll(".course-dropdown").forEach((d) => d.classList.remove("open"));
    }
  });

  async function loadHaveNeedPosts() {
    const q = (document.getElementById("hnFeedSearch")?.value || "").trim();
    const mineParam = state.hnOnlyMine ? "&mine=1" : "";
    try {
      const data = await apiFetch(`/api/have-need?q=${encodeURIComponent(q)}${mineParam}`);
      state.hnPosts = data.posts || [];
      renderHaveNeedPosts();
    } catch (err) {
      if (state.hnOnlyMine && !state.user) {
        openAuthModal("login");
        setHaveNeedFilter(false);
        return;
      }
      showToast(err.message, true);
    }
  }

  function setHaveNeedFilter(onlyMine) {
    if (onlyMine && !state.user) {
      showToast("Please sign in to view and manage your own posts.", true);
      openAuthModal("login");
      return;
    }
    state.hnOnlyMine = onlyMine;
    document.getElementById("hnTabAll").classList.toggle("active", !onlyMine);
    document.getElementById("hnTabMine").classList.toggle("active", onlyMine);
    loadHaveNeedPosts();
  }

  function formatContactLink(dm) {
    const clean = (dm || "").trim();
    if (clean.startsWith("@")) {
      const handle = clean.slice(1);
      return `<a href="https://t.me/${encodeURIComponent(handle)}" target="_blank" rel="noopener noreferrer" class="btn-illuminated" style="padding:7px 14px; font-size:12.5px;">DM ${escapeHtml(clean)}</a>`;
    }
    if (clean.startsWith("https://t.me/") || clean.startsWith("http://t.me/") || clean.startsWith("t.me/")) {
      const url = clean.startsWith("http") ? clean : `https://${clean}`;
      return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="btn-illuminated" style="padding:7px 14px; font-size:12.5px;">Contact on Telegram</a>`;
    }
    return `<span class="semester-pill" style="font-size:12px;">${escapeHtml(clean)}</span>`;
  }

  function renderHaveNeedPosts() {
    const container = document.getElementById("haveNeedList");
    if (!container) return;

    if (state.hnPosts.length === 0) {
      container.innerHTML = `
      <div class="glass-card" style="text-align:center; padding:42px 20px;">
        <div style="font-size:36px; margin-bottom:10px;">🔄</div>
        <h3 style="font-size:18px; font-weight:700;">No Have-Need Exchanges Found</h3>
        <p style="color:var(--text-secondary); font-size:13.5px; margin-top:4px;">
          ${state.hnOnlyMine ? "You haven't posted any course swaps yet." : "Be the first to post a course section swap on the left!"}
        </p>
      </div>
    `;
      return;
    }

    container.innerHTML = state.hnPosts
      .map((p) => {
        const hc = p.have_course || {};
        const nc = p.need_course || {};
        const authorObj = {
          id: p.author?.id || p.web_user_id,
          name: p.author?.name || "AUA Student",
          avatar_url: p.author?.avatar_url || "",
          bio: p.author?.bio || "",
          inbox_link: p.author?.inbox_link || "",
          is_bot_synced: p.author?.is_bot_synced,
          is_alumni: p.author?.is_alumni,
        };
        const ownerControls = p.is_owner
          ? `
          <div style="display:flex; gap:8px;">
            <button class="btn-glass" style="padding:6px 12px; font-size:12px;" onclick="startEditHaveNeed(${p.id})">✏️ Edit</button>
            <button class="btn-danger-glass" onclick="deleteHaveNeed(${p.id})">🗑️ Remove</button>
          </div>
        `
          : "";

        return `
        <div class="glass-card">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;">
            <div style="display:flex; align-items:center; gap:12px;">
              ${renderAvatarCircleHtml(authorObj, "user-avatar-md", true)}
              <div>
                <div>${renderClickableUserNameHtml(authorObj, "hn-author-name")}</div>
                <div style="font-size:11.5px; color:var(--text-secondary); margin-top:1px;">${formatDateTime(p.created_at)}</div>
              </div>
            </div>
            <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
              ${formatContactLink(p.contact_dm)}
              ${ownerControls}
            </div>
          </div>

          <div class="hn-exchange-row">
            <div class="hn-course-box have-box">
              <div class="hn-badge-label">📘 HAVE</div>
              <div style="font-size:15px; font-weight:800; color:var(--text-white);">
                ${escapeHtml(hc.course_code)} <span style="color:var(--neon-blue-bright);">[Sec ${escapeHtml(hc.section)}]</span>
              </div>
              <div style="font-size:13px; color:var(--text-ice); margin-top:2px;">${escapeHtml(hc.course_title)}</div>
              <div style="font-size:12px; color:var(--text-secondary); margin-top:6px;">
                ${escapeHtml(hc.instructor)}<br/>🕒 ${escapeHtml(formatScheduleTimes(hc.schedule_times))}
              </div>
            </div>

            <div class="hn-swap-arrow">⇄</div>

            <div class="hn-course-box need-box">
              <div class="hn-badge-label">📗 NEED</div>
              <div style="font-size:15px; font-weight:800; color:var(--text-white);">
                ${escapeHtml(nc.course_code)} <span style="color:var(--emerald);">[Sec ${escapeHtml(nc.section)}]</span>
              </div>
              <div style="font-size:13px; color:var(--text-ice); margin-top:2px;">${escapeHtml(nc.course_title)}</div>
              <div style="font-size:12px; color:var(--text-secondary); margin-top:6px;">
                ${escapeHtml(nc.instructor)}<br/>🕒 ${escapeHtml(formatScheduleTimes(nc.schedule_times))}
              </div>
            </div>
          </div>

          ${p.notes
            ? `<div style="margin-top:8px; padding:8px 12px; border-radius:8px; background:rgba(56,189,248,0.07); font-size:12.5px; color:var(--text-ice);">💬 <b>Note:</b> ${escapeHtml(p.notes)}</div>`
            : ""
          }
        </div>
      `;
      })
      .join("");
  }

  async function submitHaveNeedForm(e) {
    e.preventDefault();
    if (!state.user) {
      showToast("Please sign in with your @edu.aua.am account to post a course swap.", true);
      openAuthModal("login");
      return;
    }
    if (!state.selectedHaveCourse || !state.selectedNeedCourse) {
      showToast("Please select both the course section you HAVE and the one you NEED.", true);
      return;
    }

    const editingId = document.getElementById("hnEditingPostId").value;
    const contact_dm = document.getElementById("hnContactDm").value.trim();
    const notes = document.getElementById("hnNotes").value.trim();

    const payload = {
      have_course_id: state.selectedHaveCourse.id,
      need_course_id: state.selectedNeedCourse.id,
      contact_dm,
      notes,
    };

    try {
      if (editingId) {
        await apiFetch(`/api/have-need/${editingId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        showToast("Your Have-Need listing has been updated!");
      } else {
        await apiFetch("/api/have-need", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        showToast("Have-Need exchange published! 🚀");
      }
      resetHaveNeedForm();
      loadHaveNeedPosts();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function startEditHaveNeed(postId) {
    const post = state.hnPosts.find((p) => p.id === postId);
    if (!post) return;
    openMobileFormPanel("hnFormPanel", "hnMobileFormToggleBtn", "Hide Course Swap Form");
    document.getElementById("hnEditingPostId").value = String(post.id);
    document.getElementById("hnFormTitle").textContent = "Edit Your Course Swap";
    document.getElementById("hnSubmitBtn").textContent = "Save Changes";
    document.getElementById("hnCancelEditBtn").style.display = "inline-flex";

    state.selectedHaveCourse = post.have_course;
    state.selectedNeedCourse = post.need_course;
    renderSelectedCourseChip("have");
    renderSelectedCourseChip("need");

    document.getElementById("hnContactDm").value = post.contact_dm || "";
    document.getElementById("hnNotes").value = post.notes || "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function resetHaveNeedForm() {
    document.getElementById("hnEditingPostId").value = "";
    document.getElementById("hnFormTitle").textContent = "Post a Course Swap";
    document.getElementById("hnSubmitBtn").textContent = "Publish Have-Need";
    document.getElementById("hnCancelEditBtn").style.display = "none";
    state.selectedHaveCourse = null;
    state.selectedNeedCourse = null;
    renderSelectedCourseChip("have");
    renderSelectedCourseChip("need");
    document.getElementById("hnContactDm").value = "";
    document.getElementById("hnNotes").value = "";
    closeMobileFormPanel("hnFormPanel", "hnMobileFormToggleBtn", "+ Post a Course Swap");
  }

  async function deleteHaveNeed(postId) {
    if (!confirm("Remove this Have-Need listing?")) return;
    try {
      await apiFetch(`/api/have-need/${postId}`, { method: "DELETE" });
      showToast("Have-Need post removed.");
      loadHaveNeedPosts();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* --------------------------------------------------------------------------
     3. <Anonymous> Indexed Posts & Synced Bot Inbox
     -------------------------------------------------------------------------- */

  let anonSearchDebounceTimer = null;
  function handleAnonPostsSearch(val) {
    const clearBtn = document.getElementById("anonPostsSearchClear");
    if (clearBtn) clearBtn.style.display = val ? "block" : "none";
    clearTimeout(anonSearchDebounceTimer);
    anonSearchDebounceTimer = setTimeout(() => {
      state.anonPostsSearch = (val || "").trim();
      if (state.anonPostsSearch) {
        trackWebEvent("search_anon_posts", state.anonPostsSearch);
      }
      loadAnonymousPosts(1);
    }, 220);
  }

  function clearAnonPostsSearch() {
    const inp = document.getElementById("anonPostsSearchInput");
    const clearBtn = document.getElementById("anonPostsSearchClear");
    if (inp) inp.value = "";
    if (clearBtn) clearBtn.style.display = "none";
    state.anonPostsSearch = "";
    loadAnonymousPosts(1);
  }

  let inboxSearchDebounceTimer = null;
  function handleInboxSearch(val) {
    const clearBtn = document.getElementById("inboxSearchClear");
    if (clearBtn) clearBtn.style.display = val ? "block" : "none";
    clearTimeout(inboxSearchDebounceTimer);
    inboxSearchDebounceTimer = setTimeout(() => {
      state.inboxSearch = (val || "").trim();
      if (state.inboxSearch) {
        trackWebEvent("search_inbox", state.inboxSearch);
      }
      loadAnonymousInbox();
    }, 220);
  }

  function clearInboxSearch() {
    const inp = document.getElementById("inboxSearchInput");
    const clearBtn = document.getElementById("inboxSearchClear");
    if (inp) inp.value = "";
    if (clearBtn) clearBtn.style.display = "none";
    state.inboxSearch = "";
    loadAnonymousInbox();
  }

  function switchAnonymousSubtab(subtab) {
    state.anonSubtab = subtab;
    document.getElementById("anonTabPosts").classList.toggle("active", subtab === "posts");
    document.getElementById("anonTabInbox").classList.toggle("active", subtab === "inbox");
    document.getElementById("anonPostsView").style.display = subtab === "posts" ? "block" : "none";
    document.getElementById("anonInboxView").style.display = subtab === "inbox" ? "block" : "none";
    if (subtab === "posts") {
      loadAnonymousPosts(state.anonPostsPage);
    } else {
      loadAnonymousInbox();
    }
  }

  async function loadAnonymousPosts(page = 1) {
    state.anonPostsPage = page;
    const grid = document.getElementById("anonPostsGrid");
    const pag = document.getElementById("anonPostsPagination");
    try {
      const qParam = state.anonPostsSearch ? `&q=${encodeURIComponent(state.anonPostsSearch)}` : "";
      const data = await apiFetch(`/api/anonymous/posts?page=${page}&per_page=20${qParam}`);
      const posts = data.posts || [];
      if (posts.length === 0) {
        grid.innerHTML = `
        <div class="glass-card" style="text-align:center; padding:42px 20px;">
          <div style="font-size:36px; margin-bottom:10px;">${state.anonPostsSearch ? "🔍" : "🎭"}</div>
          <h3 style="font-size:18px; font-weight:700;">${state.anonPostsSearch ? "No Posts Found" : "No Anonymous Posts Yet"}</h3>
          <p style="color:var(--text-secondary); font-size:13.5px; margin-top:6px;">
            ${state.anonPostsSearch ? `No posts matched "<b>${escapeHtml(state.anonPostsSearch)}</b>". Try searching by #id or different keywords.` : `Open <a href="https://t.me/aua_anonymous_bot" target="_blank" style="color:var(--neon-blue-bright); font-weight:700;">@aua_anonymous_bot</a> on Telegram and type <code>/post</code> to publish the first indexed anonymous post!`}
          </p>
        </div>
      `;
        pag.innerHTML = "";
        return;
      }

      grid.innerHTML = posts
        .map(
          (p) => `
        <div class="glass-card anon-post-card">
          <div class="anon-post-header">
            <span class="anon-index-badge">#${p.index}</span>
            <span style="font-size:12px; color:var(--text-secondary);">🕒 ${formatDateTime(p.created_at)}</span>
          </div>
          <div style="font-size:15px; color:var(--text-white); white-space:pre-wrap; line-height:1.6;">${escapeHtml(p.content_text)}</div>
        </div>
      `
        )
        .join("");

      renderPagination(pag, data.page, data.total_pages, "loadAnonymousPosts");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function loadAnonymousInbox() {
    const container = document.getElementById("anonInboxContainer");
    if (!container) return;

    if (!state.user) {
      container.innerHTML = `
      <div class="glass-card" style="text-align:center; padding:42px 20px;">
        <div style="font-size:36px; margin-bottom:10px;">🔒</div>
        <h3 style="font-size:18px; font-weight:700;">Sign In to Access Your Personal Inbox</h3>
        <p style="color:var(--text-secondary); font-size:13.5px; margin:8px auto 18px; max-width:460px;">
          Sign in with your <code>@edu.aua.am</code> or <code>@alumni.aua.am</code> account to sync your personal <code>/link</code> inbox from <b>@aua_anonymous_bot</b>.
        </p>
        <button class="btn-illuminated" onclick="openAuthModal('login')">Sign In / Register</button>
      </div>
    `;
      return;
    }

    try {
      const qParam = state.inboxSearch ? `?q=${encodeURIComponent(state.inboxSearch)}` : "";
      const data = await apiFetch(`/api/anonymous/inbox${qParam}`);
      const syncBannerHtml = `
      <div class="glass-panel" style="padding:22px; margin-bottom:20px; display:flex; align-items:center; justify-content:space-between; gap:18px; flex-wrap:wrap;">
        <div>
          <div style="display:flex; align-items:center; gap:10px;">
            <h3 style="font-size:18px; font-weight:800;">
              ${data.is_synced ? "✅ Synced with @aua_anonymous_bot" : "🔄 Sync Your Personal Inbox with the Bot"}
            </h3>
          </div>
          <p style="font-size:13.5px; color:var(--text-secondary); margin-top:4px; max-width:660px;">
            ${data.is_synced
          ? `Your personal inbox link (<code style="color:var(--neon-blue-bright);">${escapeHtml(data.inbox_link || "")}</code>) is live! Messages sent to your <code>/link</code> appear right below.`
          : `Click <b>"Sync with the Bot"</b> to get a 6-digit code, then run <code>/sync</code> in <b>@aua_anonymous_bot</b>. Syncing also automatically verifies your AUA student &amp; gender status in the bot!`
        }
          </p>
          <div id="syncCodeBoxArea"></div>
        </div>
        <div style="display:flex; gap:10px; flex-wrap:wrap;">
          <button class="btn-illuminated" onclick="requestBotSyncCode()">
            🔗 ${data.is_synced ? "Re-Sync / New Code" : "Sync with the Bot"}
          </button>
          <button class="btn-glass" onclick="loadAnonymousInbox()">🔄 Refresh Inbox</button>
        </div>
      </div>
    `;

      const msgs = data.messages || [];
      let messagesHtml = "";
      if (!data.is_synced) {
        messagesHtml = `
        <div class="glass-card" style="text-align:center; padding:36px 20px;">
          <p style="color:var(--text-secondary); font-size:14px;">
            Your account is not synced with <b>@aua_anonymous_bot</b> yet. Click <b>"Sync with the Bot"</b> above to connect your inbox!
          </p>
        </div>
      `;
      } else if (msgs.length === 0) {
        messagesHtml = `
        <div class="glass-card" style="text-align:center; padding:36px 20px;">
          <div style="font-size:32px; margin-bottom:8px;">${state.inboxSearch ? "🔍" : "📭"}</div>
          <h4 style="font-size:16px; font-weight:700;">${state.inboxSearch ? "No Matching Messages Found" : "No Anonymous Inbox Messages Yet"}</h4>
          <p style="color:var(--text-secondary); font-size:13.5px; margin-top:6px;">
            ${state.inboxSearch ? `No messages matched "<b>${escapeHtml(state.inboxSearch)}</b>". Try searching by another keyword or message ID.` : `Share your personal link (<code>${escapeHtml(data.inbox_link || "")}</code>) on your profile or social bio to receive anonymous questions!`}
          </p>
        </div>
      `;
      } else {
        messagesHtml = `
        <div class="hn-cards-grid">
          ${msgs
            .map(
              (m) => `
              <div class="glass-card">
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
                  <span class="anon-index-badge">💌 Anonymous Message</span>
                  <span style="font-size:12px; color:var(--text-secondary);">${formatDateTime(m.created_at)}</span>
                </div>
                <div style="font-size:15px; color:var(--text-white); white-space:pre-wrap; margin:10px 0;">${escapeHtml(m.content_text)}</div>
                ${m.reply_text
                  ? `<div style="margin-top:10px; padding:10px 14px; border-radius:10px; background:rgba(56,189,248,0.1); border-left:3px solid var(--neon-blue); font-size:13.5px; color:var(--text-ice);">
                        <b>↪ Your Reply:</b> ${escapeHtml(m.reply_text)}
                       </div>`
                  : `<div style="margin-top:12px; display:flex; gap:8px;">
                        <input type="text" id="inboxReplyInput_${m.id}" class="input-glass" placeholder="Write an anonymous reply to sender via Telegram..." />
                        <button class="btn-illuminated" onclick="sendInboxReply(${m.id})">Reply ↩</button>
                       </div>`
                }
              </div>
            `
            )
            .join("")}
        </div>
      `;
      }

      container.innerHTML = syncBannerHtml + messagesHtml;
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function requestBotSyncCode() {
    try {
      const res = await apiFetch("/api/anonymous/sync-code", { method: "POST" });
      const box = document.getElementById("syncCodeBoxArea");
      if (box) {
        box.innerHTML = `
        <div style="margin-top:14px; padding:16px; border-radius:14px; background:rgba(5,9,20,0.85); border:1px solid var(--neon-blue);">
          <div style="font-size:13px; color:var(--text-ice);">
            1️⃣ Open <b>@aua_anonymous_bot</b> and send <code>/sync</code>, then type this 6-digit code:
          </div>
          <div class="sync-code-display">${escapeHtml(res.code)}</div>
          <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
            <a href="${escapeHtml(res.deep_link)}" target="_blank" rel="noopener noreferrer" class="btn-illuminated">
              ⚡ Open @aua_anonymous_bot &amp; Auto-Sync Now
            </a>
            <span style="font-size:12px; color:var(--text-secondary);">Expires in 15 minutes. After syncing in Telegram, click "Refresh Inbox".</span>
          </div>
        </div>
      `;
      }
      showToast("Sync code generated! Enter it via /sync in @aua_anonymous_bot.");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function sendInboxReply(messageId) {
    const input = document.getElementById(`inboxReplyInput_${messageId}`);
    if (!input) return;
    const reply_text = input.value.trim();
    if (!reply_text) {
      showToast("Please type a reply first.", true);
      return;
    }
    try {
      await apiFetch("/api/anonymous/inbox/reply", {
        method: "POST",
        body: JSON.stringify({ message_id: messageId, reply_text }),
      });
      showToast("Reply delivered to sender via @aua_anonymous_bot! ✨");
      loadAnonymousInbox();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* --------------------------------------------------------------------------
     4. <Free Rooms> Live Main Building & PAB Room Finder
     -------------------------------------------------------------------------- */

  let _freeRoomsCooldownTimer = null;

  function setRefreshButtonCooldown(seconds) {
    const btn = document.getElementById("freeRoomsRefreshBtn");
    if (!btn) return;
    if (_freeRoomsCooldownTimer) clearInterval(_freeRoomsCooldownTimer);
    btn.disabled = true;
    btn.style.opacity = "0.6";
    btn.style.cursor = "not-allowed";

    let remaining = seconds;
    btn.textContent = `⏳ Wait ${remaining}s`;

    _freeRoomsCooldownTimer = setInterval(() => {
      remaining--;
      if (remaining <= 0) {
        clearInterval(_freeRoomsCooldownTimer);
        _freeRoomsCooldownTimer = null;
        btn.disabled = false;
        btn.style.opacity = "";
        btn.style.cursor = "";
        btn.textContent = "Refresh Live Table";
      } else {
        btn.textContent = `⏳ Wait ${remaining}s`;
      }
    }, 1000);
  }

  function setDateInputConstraints() {
    const dateInput = document.getElementById("freeRoomsDate");
    if (!dateInput) return;
    const now = new Date();
    const yest = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    const max = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000);
    dateInput.min = yest.toISOString().split("T")[0];
    dateInput.max = max.toISOString().split("T")[0];
  }

  async function loadFreeRooms(forceRefresh = false) {
    const grid = document.getElementById("freeRoomsGrid");
    const dateInput = document.getElementById("freeRoomsDate");
    setDateInputConstraints();
    const dateVal = dateInput ? dateInput.value : "";
    const cacheKey = `aua_free_rooms_${dateVal || "today"}`;

    const refreshBtn = document.getElementById("freeRoomsRefreshBtn");
    if (forceRefresh && refreshBtn) {
      if (refreshBtn.disabled) return;
      refreshBtn.disabled = true;
      refreshBtn.textContent = "⏳ Scanning...";
    }

    // 1. Instant Cache Check: in-memory state or browser sessionStorage (0ms)
    let cachedObj = null;
    if (!forceRefresh) {
      if (state.freeRoomsPayload && (!dateVal || state.freeRoomsPayload.date === dateVal)) {
        renderFreeRooms();
        return;
      }
      try {
        const stored = sessionStorage.getItem(cacheKey);
        if (stored) cachedObj = JSON.parse(stored);
      } catch { }

      if (cachedObj && cachedObj.data) {
        state.freeRoomsPayload = cachedObj.data;
        if (dateInput && !dateInput.value && cachedObj.data.date) {
          dateInput.value = cachedObj.data.date;
        }
        renderFreeRooms();

        // If client cache is fresh (< 10 minutes), avoid network call completely!
        const ageMs = Date.now() - (cachedObj.ts || 0);
        if (ageMs < 10 * 60 * 1000) {
          return;
        }
        // If older, continue in background without clearing the grid or showing a spinner
      }
    }

    // Only display the loading card if we have nothing on screen
    if (grid && !state.freeRoomsPayload && !cachedObj) {
      grid.innerHTML = `
      <div class="glass-card" style="grid-column: 1 / -1; text-align:center; padding:42px 20px;">
        <div style="font-size:32px; margin-bottom:10px;">⏳</div>
        <h3 style="font-size:17px; font-weight:700;">Scanning Live Room Timetable from booking.aua.am...</h3>
        <p style="color:var(--text-secondary); font-size:13px; margin-top:4px;">Fetching Main Building &amp; PAB schedules and filtering out 10-minute class gaps.</p>
      </div>
    `;
    }

    try {
      const params = new URLSearchParams();
      if (dateVal) params.set("date", dateVal);
      if (forceRefresh) params.set("refresh", "1");
      const data = await apiFetch(`/api/free-rooms?${params.toString()}`);
      state.freeRoomsPayload = data;
      try {
        sessionStorage.setItem(cacheKey, JSON.stringify({ data, ts: Date.now() }));
      } catch { }
      if (dateInput && !dateInput.value && data.date) {
        dateInput.value = data.date;
      }
      renderFreeRooms();
      if (forceRefresh) {
        showToast("Live room timetable refreshed!");
        setRefreshButtonCooldown(20);
      }
    } catch (err) {
      if (grid && !state.freeRoomsPayload) {
        grid.innerHTML = `<div class="glass-card" style="grid-column:1/-1; text-align:center; color:var(--danger);">${escapeHtml(err.message)}</div>`;
      }
      showToast(err.message, true);
      if (forceRefresh) {
        setRefreshButtonCooldown(10);
      }
    }
  }

  function setBuildingFilter(bKey) {
    state.selectedBuilding = bKey;
    document.querySelectorAll("[data-building]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-building") === bKey);
    });
    renderFreeRooms();
  }

  function setRoomAvailabilityFilter(mode) {
    state.roomAvailFilter = mode || "all";
    document.querySelectorAll("[data-avail]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-avail") === state.roomAvailFilter);
    });
    renderFreeRooms();
  }

  function parseHhMmToMinutes(str) {
    if (!str || typeof str !== "string") return null;
    const parts = str.trim().split(":");
    if (parts.length < 2) return null;
    const h = parseInt(parts[0], 10);
    const m = parseInt(parts[1], 10);
    if (isNaN(h) || isNaN(m)) return null;
    return h * 60 + m;
  }

  function formatRemainingMinutes(mins) {
    if (!mins || mins <= 0) return "";
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (h > 0 && m > 0) return `${h}h ${m}m left`;
    if (h > 0) return `${h}h left`;
    return `${m}m left`;
  }

  function getRoomRemainingMinutesNow(room, nowMinutes) {
    if (!room || !room.is_free_now) return 0;
    const activeWin = (room.free_windows || []).find((w) => w.is_now);
    if (activeWin) {
      if (nowMinutes !== null && typeof activeWin.end_minutes === "number") {
        return Math.max(0, activeWin.end_minutes - nowMinutes);
      }
      return Number(activeWin.duration_minutes) || 0;
    }
    const untilMins = parseHhMmToMinutes(room.free_until);
    if (nowMinutes !== null && untilMins !== null) {
      return Math.max(0, untilMins - nowMinutes);
    }
    return 0;
  }

  function renderFreeRooms() {
    const grid = document.getElementById("freeRoomsGrid");
    const summaryEl = document.getElementById("freeRoomsSummary");
    if (!grid || !state.freeRoomsPayload) return;

    const data = state.freeRoomsPayload;
    const availMode = state.roomAvailFilter || "all";
    const searchQ = (document.getElementById("roomSearchInput")?.value || "").trim().toLowerCase();
    const nowMinutes = parseHhMmToMinutes(data.current_time_yerevan);

    let rooms = [];
    if (state.selectedBuilding === "main") {
      rooms = data.buildings?.main?.rooms || [];
    } else if (state.selectedBuilding === "pab") {
      rooms = data.buildings?.pab?.rooms || [];
    } else {
      rooms = [...(data.buildings?.main?.rooms || []), ...(data.buildings?.pab?.rooms || [])];
    }

    rooms = rooms.filter((r) => {
      if (availMode === "now" && !r.is_free_now) return false;
      if (availMode === "1h") {
        if (!r.is_free_now) return false;
        const rem = getRoomRemainingMinutesNow(r, nowMinutes);
        if (rem < 60) return false;
      }
      if (searchQ && !`${r.room_name} ${r.building_name}`.toLowerCase().includes(searchQ)) return false;
      return true;
    });

    if (summaryEl) {
      summaryEl.innerHTML = `
      📅 <b>${escapeHtml(data.date_display || data.date)}</b> &bull;
      🕒 Yerevan Time: <b>${escapeHtml(data.current_time_yerevan || "")}</b> &bull;
      🟢 <b>${data.summary?.free_now_count || 0}</b> rooms free right now
      (Main: <b>${data.summary?.main_free_now || 0}</b>, PAB: <b>${data.summary?.pab_free_now || 0}</b>) &bull;
      ⏱️ Free intervals &ge; 20 mins
    `;
    }

    if (rooms.length === 0) {
      grid.innerHTML = `
      <div class="glass-card" style="grid-column:1/-1; text-align:center; padding:36px;">
        <h3 style="font-size:17px;">No matching rooms found for the selected filter.</h3>
      </div>
    `;
      return;
    }

    grid.innerHTML = rooms
      .map((r) => {
        const remMins = getRoomRemainingMinutesNow(r, nowMinutes);
        const remLabel = remMins > 0 ? ` (${formatRemainingMinutes(remMins)})` : "";
        const statusBadge = r.is_free_now
          ? `<span class="status-pill-free">🟢 Free until ${escapeHtml(r.free_until)}${escapeHtml(remLabel)}</span>`
          : `<span class="status-pill-busy">Scheduled / Busy Now</span>`;

        const windowsHtml =
          r.free_windows && r.free_windows.length > 0
            ? r.free_windows
              .map(
                (w) => `
              <span class="time-window-chip ${w.is_now ? "active-now" : ""}">
                <span>🕒 ${escapeHtml(w.label)}</span>
                <span class="time-window-duration">(${escapeHtml(w.duration_label)})</span>
              </span>
            `
              )
              .join("")
            : `<span style="font-size:12.5px; color:var(--text-secondary);">Booked all day</span>`;

        return `
        <div class="glass-card room-card">
          <div class="room-card-header">
            <div class="room-number">
              <span>🚪 ${escapeHtml(r.room_name)}</span>
            </div>
            ${statusBadge}
          </div>
          <div class="time-windows-list">
            ${windowsHtml}
          </div>
        </div>
      `;
      })
      .join("");
  }

  /* --------------------------------------------------------------------------
     5. <Telegram Groups> Campus Directory
     -------------------------------------------------------------------------- */

  async function loadTelegramGroups(page = 1) {
    state.groupsPage = page;
    const q = (document.getElementById("groupSearchInput")?.value || "").trim();
    const grid = document.getElementById("groupsGrid");
    const pag = document.getElementById("groupsPagination");
    if (!grid) return;

    try {
      const data = await apiFetch(`/api/groups?q=${encodeURIComponent(q)}&page=${page}&per_page=9`);
      const groups = data.groups || [];
      if (groups.length === 0) {
        grid.innerHTML = `
        <div class="glass-card" style="grid-column:1/-1; text-align:center; padding:42px 20px;">
          <div style="font-size:36px; margin-bottom:10px;">✈️</div>
          <h3 style="font-size:18px; font-weight:700;">No Telegram Groups Found</h3>
          <p style="color:var(--text-secondary); font-size:13.5px; margin-top:4px;">
            Have a study group, major chat, or student club? Click <b>"Post Telegram Group"</b> to add it!
          </p>
        </div>
      `;
        if (pag) pag.innerHTML = "";
        return;
      }

      grid.innerHTML = groups
        .map((g) => {
          const initial = (g.name || "G").trim().charAt(0).toUpperCase();
          const imgHtml = g.image_url
            ? `<img src="${escapeHtml(g.image_url)}" class="group-avatar" alt="${escapeHtml(g.name)}" />`
            : `<div class="group-avatar">${escapeHtml(initial)}</div>`;
          const authorObj = {
            id: g.author_id || g.web_user_id,
            name: g.author_name || "AUA Student",
            avatar_url: g.author_avatar || "",
            bio: g.author_bio || "",
            inbox_link: g.author_inbox_link || "",
          };

          return `
          <div class="glass-card group-card">
            <div>
              <div class="group-card-top">
                ${imgHtml}
                <div style="flex:1;">
                  <h3 style="font-size:17px; font-weight:800; color:var(--text-white);">${escapeHtml(g.name)}</h3>
                  <div style="font-size:12px; color:var(--text-secondary); margin-top:4px; display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                    <span>Posted by</span>
                    ${renderAvatarCircleHtml(authorObj, "user-avatar-xs", true)}
                    ${renderClickableUserNameHtml(authorObj, "group-author-name")}
                  </div>
                </div>
              </div>
              <p style="font-size:13.5px; color:var(--text-ice); margin-top:12px; line-height:1.5;">
                ${escapeHtml(g.description || "AUA student Telegram community.")}
              </p>
            </div>

            <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:6px;">
              <a href="${escapeHtml(g.invite_link)}" target="_blank" rel="noopener noreferrer" class="btn-illuminated" style="flex:1;" onclick="trackWebEvent('join_group', '${encodeJsAttr(g.name)}')">
                ✈️ Join Group
              </a>
              ${g.is_owner
              ? `<button class="btn-danger-glass" onclick="deleteGroup(${g.id})" title="Delete Group">🗑️</button>`
              : ""
            }
            </div>
          </div>
        `;
        })
        .join("");

      renderPagination(pag, data.page, data.total_pages, "loadTelegramGroups");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function openGroupModal() {
    if (!state.user) {
      showToast("Please sign in to post a Telegram group.", true);
      openAuthModal("login");
      return;
    }
    updateGroupModalPreview();
    document.getElementById("groupModal").classList.add("open");
    if (window.innerWidth > 768) {
      setTimeout(() => document.getElementById("groupNameInput")?.focus(), 80);
    }
  }

  function closeGroupModal() {
    document.getElementById("groupModal")?.classList.remove("open");
  }

  function updateGroupModalPreview() {
    const nameVal = (document.getElementById("groupNameInput")?.value || "").trim();
    const initialEl = document.getElementById("groupAvatarInitial");
    if (initialEl) {
      initialEl.textContent = nameVal ? getUserInitial(nameVal) : "✈️";
    }
  }

  function clearGroupUploadedPhoto() {
    const urlInput = document.getElementById("groupImageUrlInput");
    const fileInput = document.getElementById("groupFileInput");
    const previewImg = document.getElementById("groupAvatarPreviewImg");
    const initialEl = document.getElementById("groupAvatarInitial");
    const removeBtn = document.getElementById("groupRemovePhotoBtn");
    const statusLabel = document.getElementById("groupUploadedPreview");

    if (urlInput) urlInput.value = "";
    if (fileInput) fileInput.value = "";
    if (previewImg) {
      previewImg.src = "";
      previewImg.style.display = "none";
    }
    if (initialEl) initialEl.style.display = "flex";
    if (removeBtn) removeBtn.style.display = "none";
    if (statusLabel) statusLabel.textContent = "Click avatar or button to upload photo";
    updateGroupModalPreview();
  }

  function appendGroupQuickTag(tagText) {
    const descEl = document.getElementById("groupDescInput");
    if (!descEl || !tagText) return;
    const current = descEl.value.trim();
    if (current.includes(tagText)) {
      descEl.focus();
      return;
    }
    descEl.value = current ? `${current} • ${tagText}` : `${tagText}: `;
    descEl.focus();
  }

  async function handleGroupImageUpload(inputEl) {
    const file = inputEl.files?.[0];
    if (!file) return;
    const preview = document.getElementById("groupUploadedPreview");
    const previewImg = document.getElementById("groupAvatarPreviewImg");
    const initialEl = document.getElementById("groupAvatarInitial");
    const removeBtn = document.getElementById("groupRemovePhotoBtn");

    if (preview) preview.textContent = "⏳ Uploading photo...";
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await apiFetch("/api/upload", { method: "POST", body: fd });
      document.getElementById("groupImageUrlInput").value = res.url;
      if (previewImg) {
        previewImg.src = res.url;
        previewImg.style.display = "block";
      }
      if (initialEl) initialEl.style.display = "none";
      if (removeBtn) removeBtn.style.display = "inline-flex";
      if (preview) preview.textContent = "✅ Group photo ready";
    } catch (err) {
      if (preview) preview.textContent = "Upload failed";
      showToast(err.message, true);
    }
  }

  async function submitNewGroup(e) {
    e.preventDefault();
    const name = document.getElementById("groupNameInput").value.trim();
    const invite_link = document.getElementById("groupLinkInput").value.trim();
    const description = document.getElementById("groupDescInput").value.trim();
    const image_url = document.getElementById("groupImageUrlInput").value.trim();

    try {
      await apiFetch("/api/groups", {
        method: "POST",
        body: JSON.stringify({ name, invite_link, description, image_url }),
      });
      closeGroupModal();
      document.getElementById("groupNameInput").value = "";
      document.getElementById("groupLinkInput").value = "";
      document.getElementById("groupDescInput").value = "";
      clearGroupUploadedPhoto();
      showToast("Telegram group published! ✈️");
      loadTelegramGroups(1);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteGroup(groupId) {
    if (!confirm("Delete this Telegram group listing?")) return;
    try {
      await apiFetch(`/api/groups/${groupId}`, { method: "DELETE" });
      showToast("Group removed.");
      loadTelegramGroups(state.groupsPage);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* --------------------------------------------------------------------------
     6. <Chat> Real-Time Campus Chat, Rich Visual Composer, Emojis & Media
     -------------------------------------------------------------------------- */

  const EMOJI_CATEGORIES = [
    { id: "smileys", icon: "😀", label: "Smileys & Emotion" },
    { id: "gestures", icon: "👋", label: "People & Gestures" },
    { id: "hearts", icon: "❤️", label: "Hearts & Symbols" },
    { id: "animals", icon: "🐶", label: "Animals & Nature" },
    { id: "food", icon: "🍕", label: "Food & Drink" },
    { id: "activities", icon: "⚽", label: "Activities & Sports" },
    { id: "travel", icon: "🚗", label: "Travel & Places" },
    { id: "objects", icon: "💡", label: "Objects" },
    { id: "flags", icon: "🇦🇲", label: "Flags" },
  ];

  const EMOJI_DATABASE = {
    smileys: [
      ["😀", "Grinning Face", "grin happy smile smileys joyful"],
      ["😃", "Grinning Face Big Eyes", "happy smile exciting joyful"],
      ["😄", "Grinning Smiling Eyes", "laugh smile laugh fun"],
      ["😁", "Beaming Face", "teeth smile beaming proud"],
      ["😆", "Grinning Squinting Face", "laughing haha lol giggle"],
      ["😅", "Grinning Face Sweat", "nervous hot sweat relief phew"],
      ["🤣", "Rolling on Floor Laughing", "rofl haha lmfao hilarious dead dying"],
      ["😂", "Face with Tears of Joy", "crying laughing joy lol lmao haha"],
      ["🙂", "Slightly Smiling Face", "smile ok fine nice pleasant"],
      ["🙃", "Upside Down Face", "sarcasm silly ironic humor"],
      ["😉", "Winking Face", "wink flirt joke playful cheeky"],
      ["😊", "Smiling Face with Eyes", "blush warm sweet kind"],
      ["😇", "Smiling Face with Halo", "angel holy innocent blessed good"],
      ["🥰", "Smiling Face with Hearts", "love crush adore sweet fond"],
      ["😍", "Heart Eyes", "love romance heart enamored infatuated"],
      ["🤩", "Star-Struck", "star eyes wow amazing impressed dazzled"],
      ["😘", "Face Blowing a Kiss", "kiss love kissy affection mwah"],
      ["😋", "Face Savoring Food", "yum delicious tasty tongue lick"],
      ["😛", "Face with Tongue", "silly cheeky tongue out blep"],
      ["😜", "Winking Face with Tongue", "crazy party joke tease wild"],
      ["🤪", "Zany Face", "wild crazy goof party goofy wacky"],
      ["😝", "Squinting Face with Tongue", "tease prank haha playful"],
      ["🤑", "Money Mouth", "dollar rich cash profit jackpot bucks"],
      ["🤗", "Smiling Face with Open Hands", "hug friendly embrace warm welcome"],
      ["🫣", "Face with Peeking Eye", "peek shy scared cringe glimpse"],
      ["🤫", "Shushing Face", "quiet silence shh secret whisper"],
      ["🤔", "Thinking Face", "think ponder wonder hmm suspect question"],
      ["🫡", "Saluting Face", "salute respect soldier yes sir honor"],
      ["🤐", "Zipper Mouth", "secret shut quiet silent zipped"],
      ["🤨", "Raised Eyebrow", "skeptical doubt suspicious rock questioning"],
      ["😐", "Neutral Face", "meh straight blank poker face"],
      ["😑", "Expressionless", "unimpressed bored flat deadpan"],
      ["😶", "Face Without Mouth", "speechless mute silence no comment"],
      ["🫥", "Dotted Line Face", "invisible ghost ignore blend in"],
      ["😏", "Smirking Face", "smug sly clever flirt knowing"],
      ["😒", "Unamused Face", "annoyed displeased bored rolling eyes"],
      ["🙄", "Face with Rolling Eyes", "eye roll whatever duh boring"],
      ["😬", "Grimacing Face", "awkward yikes cringe teeth oof"],
      ["🤥", "Lying Face", "pinocchio lie deceit fake nose"],
      ["😌", "Relieved Face", "peace calm zen phew relaxed"],
      ["😔", "Pensive Face", "sad down depressed gloomy pensive"],
      ["😪", "Sleepy Face", "tired tear sleep snooze snot"],
      ["🤤", "Drooling Face", "drool crave food sleep hungry"],
      ["😴", "Sleeping Face", "sleep zzz bedtime snore rest"],
      ["😷", "Medical Mask", "sick doctor virus covid safe ill"],
      ["🤒", "Face with Thermometer", "sick fever ill flu temp virus"],
      ["🤕", "Head-Bandage", "hurt injured pain ouch wound head"],
      ["🤢", "Nauseated Face", "green gross sick throw up disgusted"],
      ["🤮", "Face Vomiting", "puke sick throw up barf vomit gross"],
      ["🤧", "Sneezing Face", "sneeze cold allergy tissue achoo"],
      ["🥵", "Hot Face", "sweat heat spicy boil summer hot"],
      ["🥶", "Cold Face", "freeze ice cold winter frost shivering"],
      ["🥴", "Woozy Face", "drunk tipsy dazed dizzy hungover"],
      ["😵", "Crossed-Out Eyes", "dead knocked out fainting stunned"],
      ["🤯", "Exploding Head", "mind blown shock crazy wow incredible"],
      ["🤠", "Cowboy Hat Face", "western yeehaw texas ranch hat"],
      ["🥳", "Partying Face", "party celebrate birthday fiesta horn hat"],
      ["🥸", "Disguised Face", "spy mustache glasses hidden incognito disguise"],
      ["😎", "Smiling with Sunglasses", "cool swag badass sunny chill rad"],
      ["🤓", "Nerd Face", "geek smart study nerd exam glasses books"],
      ["🧐", "Face with Monocle", "curious investigate fancy classy inspect"],
      ["😕", "Confused Face", "confused puzzled what huh lost"],
      ["😟", "Worried Face", "anxious concern troubled nervous scared"],
      ["🙁", "Slightly Frowning", "frown sad disappointment bummed"],
      ["😮", "Open Mouth", "surprised wow gasp shock amazed"],
      ["😯", "Hushed Face", "surprise quiet stunned whoa"],
      ["😲", "Astonished Face", "shocked speechless amazed jaw drop"],
      ["😳", "Flushed Face", "blush embarrassment red shock wide eyes"],
      ["🥺", "Pleading Face", "puppy eyes please beg cute sad needy"],
      ["🥹", "Holding Back Tears", "touched proud emotional weep gratitude"],
      ["😦", "Frowning Face Open Mouth", "scared stunned bad unhappy"],
      ["😨", "Fearful Face", "afraid scare fear terror panicked"],
      ["😰", "Anxious Face with Sweat", "nervous sweat worry distress blue"],
      ["😥", "Sad but Relieved", "phew close call sigh sweat relief"],
      ["😢", "Crying Face", "tear sad weep cry heartbroken"],
      ["😭", "Loudly Crying Face", "bawling weeping sobbing so sad flood tears"],
      ["😱", "Screaming in Fear", "scream horror spooky scary shock munch"],
      ["😖", "Confounded Face", "frustrated struggle groan agitated"],
      ["😣", "Persevering Face", "endure tough pain struggling"],
      ["😞", "Disappointed Face", "sad regret fail bummed upset"],
      ["😓", "Downcast Face with Sweat", "hard work stress tired effort"],
      ["😩", "Weary Face", "tired exhausted whine complain fed up"],
      ["😫", "Tired Face", "so tired give up stressed exhausted"],
      ["🥱", "Yawning Face", "sleepy yawn boring morning exhausted"],
      ["😤", "Steam from Nose", "angry triumph huff furious determination"],
      ["😡", "Pouting Face", "angry mad rage red furious outraged"],
      ["😠", "Angry Face", "mad irritated evil cross grim"],
      ["🤬", "Symbols on Mouth", "cursing swear angry rage explicit swearing"],
      ["😈", "Smiling with Horns", "devil mischievous wicked bad naughty"],
      ["👿", "Angry with Horns", "demon evil monster dark imp anger"],
      ["💀", "Skull", "dead skeleton haha dying ded death funny"],
      ["☠️", "Skull and Crossbones", "danger poison pirate death hazard warning"],
      ["💩", "Pile of Poo", "poop turd brown crap funny smile"],
      ["🤡", "Clown Face", "circus clown joke silly foolish circus"],
      ["👻", "Ghost", "spooky halloween boo haunt spirit ghost"],
      ["👽", "Alien", "ufo martian extraterrestrial outer space alien"],
      ["🤖", "Robot", "bot artificial intelligence tech automated ai"],
      ["🎃", "Jack-O-Lantern", "pumpkin halloween autumn harvest orange carved"],
      ["🔥", "Fire", "lit hot burning awesome lit flames flame lit"],
      ["✨", "Sparkles", "magic shiny sparkle twinkle stars clean glow"],
      ["💯", "Hundred Points", "perfect 100 score keep it real factual"],
    ],
    gestures: [
      ["👋", "Waving Hand", "wave hello goodbye hi hey greeting"],
      ["🤚", "Raised Back of Hand", "hand stop back raised palm"],
      ["🖐️", "Hand with Fingers Splayed", "high five palm open greeting stop"],
      ["✋", "Raised Hand", "stop halt high five pause wait"],
      ["🖖", "Vulcan Salute", "spock star trek peace live long prosper"],
      ["🫱", "Rightwards Hand", "reach hand gesture direction"],
      ["🫲", "Leftwards Hand", "reach hand gesture direction"],
      ["🫳", "Palm Down Hand", "drop dismiss pet gesture down"],
      ["🫴", "Palm Up Hand", "offer beckon summon come receive"],
      ["👌", "OK Hand", "ok okay perfect alright good fine excellent"],
      ["🤌", "Pinched Fingers", "italian what do you want mama mia gesture"],
      ["🤏", "Pinching Hand", "tiny little bit small short pinch"],
      ["✌️", "Victory Hand", "peace v victory two win celebrate"],
      ["🤞", "Crossed Fingers", "luck wish hopeful pray promise fingers"],
      ["🫰", "Hand Crossed Thumb", "finger heart money kpop snap saranghae"],
      ["🤟", "Love-You Gesture", "love you ily rock sign language deaf"],
      ["🤘", "Sign of the Horns", "rock metal party rock on cool heavy"],
      ["🤙", "Call Me Hand", "call phone shaka hang loose surf aloha"],
      ["👈", "Index Pointing Left", "left point arrow that way look"],
      ["👉", "Index Pointing Right", "right point arrow that way look"],
      ["👆", "Index Pointing Up", "up point ceiling sky this look above"],
      ["🖕", "Middle Finger", "rude offensive finger gesture fck"],
      ["👇", "Index Pointing Down", "down point below floor see look below"],
      ["☝️", "Index Pointing Upward", "point number one question idea listen"],
      ["🫵", "Index Pointing at Viewer", "you target choice point you audience"],
      ["👍", "Thumbs Up", "like approve good yes cool ok upvote great"],
      ["👎", "Thumbs Down", "dislike bad no downvote reject boo"],
      ["✊", "Raised Fist", "power solidarity punch strength revolution"],
      ["👊", "Oncoming Fist", "punch bro fist bump attack hit combat"],
      ["🤛", "Left-Facing Fist", "fist bump left pound brother"],
      ["🤜", "Right-Facing Fist", "fist bump right pound brother"],
      ["👏", "Clapping Hands", "applause bravo clap celebrate praise kudos"],
      ["🙌", "Raising Hands", "yay celebrate hooray praise ten hallelujah"],
      ["🫶", "Heart Hands", "love affection sweet heart hands caring"],
      ["👐", "Open Hands", "hug open jazz hands welcome embrace"],
      ["🤲", "Palms Up Together", "pray offering book hope dua supplication"],
      ["🤝", "Handshake", "deal agreement partner meet respect shake business"],
      ["🙏", "Folded Hands", "please thank you pray grateful namaste hope blessing"],
      ["✍️", "Writing Hand", "write pen note homework exam study author"],
      ["💅", "Nail Polish", "manicure nails slay queen drama sassy polish"],
      ["🤳", "Selfie", "phone camera photo selfie mirror pose"],
      ["💪", "Flexed Biceps", "muscle strong workout gym flex power fitness bicep"],
      ["🦾", "Mechanical Arm", "cyborg prosthetic robot arm bionic"],
      ["🧠", "Brain", "smart mind think intelligence intellect clever"],
      ["👀", "Eyes", "look see view stare watch suspicious look observe"],
      ["👁️", "Eye", "eye see watch illuminate look looker vision"],
      ["👅", "Tongue", "taste lick tongue flavor silly oral"],
      ["👄", "Mouth", "lips kiss talk lipstick speak mouth"],
      ["🧑", "Person", "human student peer individual youth"],
      ["👨‍🎓", "Student Man", "student graduate aua college degree university scholar"],
      ["👩‍🎓", "Student Woman", "student graduate aua college degree university woman alumna"],
      ["👨‍🏫", "Teacher Man", "professor instructor teacher faculty aua lecturer"],
      ["👩‍🏫", "Teacher Woman", "professor instructor teacher faculty woman aua lecturer"],
      ["👨‍💻", "Technologist Man", "coder developer programmer hacker laptop computer engineer"],
      ["👩‍💻", "Technologist Woman", "coder developer programmer hacker laptop computer woman engineer"],
    ],
    hearts: [
      ["❤️", "Red Heart", "love red romance passion heart true love"],
      ["🧡", "Orange Heart", "orange warm friendship love autumn"],
      ["💛", "Yellow Heart", "yellow sunshine loyalty best friend happiness"],
      ["💚", "Green Heart", "green eco nature envious organic safe"],
      ["💙", "Blue Heart", "blue trust aua student calm loyalty ocean"],
      ["💜", "Purple Heart", "purple royalty magic bts aesthetic lavender"],
      ["🖤", "Black Heart", "black emo goth sorrow dark metal"],
      ["🤍", "White Heart", "white pure peace wedding clear clean"],
      ["🤎", "Brown Heart", "brown earth chocolate warmth coffee"],
      ["💔", "Broken Heart", "heartbreak breakup pain dump sorrow sad tears"],
      ["❤️‍🔥", "Heart on Fire", "burning love passion lust flames hot desire"],
      ["❤️‍🩹", "Mending Heart", "healing recover better bandage heart heal"],
      ["💕", "Two Hearts", "love affection cute hearts floating"],
      ["💞", "Revolving Hearts", "dancing hearts spin in love swirl"],
      ["💓", "Beating Heart", "pulse heartbeat flutter excited love throb"],
      ["💗", "Growing Heart", "expanding warm blush excitement blooming"],
      ["💖", "Sparkling Heart", "sparkle shiny love glitter cute dazzle"],
      ["💘", "Heart with Arrow", "cupid romance struck falling in love shot"],
      ["💝", "Heart with Ribbon", "gift present valentines box wrapped love"],
      ["💟", "Heart Decoration", "purple white heart symbol badge"],
      ["☮️", "Peace Symbol", "peace hippy calm antiwar serene"],
      ["✝️", "Latin Cross", "christian cross faith church jesus"],
      ["☪️", "Star and Crescent", "islam muslim faith symbol moon"],
      ["☸️", "Wheel of Dharma", "buddhism dharma wheel faith balance"],
      ["☯️", "Yin Yang", "balance harmony taoism zen dualism"],
      ["☦️", "Orthodox Cross", "armenian apostolic orthodox cross christian faith"],
      ["⚠️", "Warning", "caution danger alert careful sign hazard risk"],
      ["⛔", "No Entry", "stop forbidden denied banned sign halt"],
      ["🚫", "Prohibited", "no banned restricted forbid cancel denied"],
      ["❌", "Cross Mark", "wrong no reject fail x error cancel incorrect"],
      ["✅", "Check Mark Button", "done correct yes pass verified success check ok agreed"],
      ["❓", "Question Mark", "what help wonder query quiz mystery ask"],
      ["❗", "Exclamation Mark", "warning alert important notice pay attention bang"],
      ["‼️", "Double Exclamation", "urgent loud bang critical notice extreme"],
      ["⁉️", "Exclamation Question", "what really huh disbelief surprise confusion"],
      ["♻️", "Recycling Symbol", "recycle green environment waste sustain eco"],
    ],
    animals: [
      ["🐶", "Dog Face", "puppy pet canine cute animal bark woof loyal"],
      ["🐱", "Cat Face", "kitty kitten feline meow pet animal cute"],
      ["🐭", "Mouse Face", "rat rodent cheese small mouse"],
      ["🐹", "Hamster Face", "cute rodent pet cheeks fluffy hamster"],
      ["🐰", "Rabbit Face", "bunny easter ears cute hopping rabbit"],
      ["🦊", "Fox Face", "fox sly red clever animal wild tail"],
      ["🐻", "Bear Face", "bear grizzly teddy forest animal brown"],
      ["🐼", "Panda Face", "panda bear bamboo cute black and white china"],
      ["🐨", "Koala Face", "koala australia eucalyptus cute bear marsupial"],
      ["🐯", "Tiger Face", "tiger cat wild predator stripe roar jungle"],
      ["🦁", "Lion Face", "lion king pride mane roar predator safari"],
      ["🐮", "Cow Face", "cow milk farm bovine moo beef livestock"],
      ["🐷", "Pig Face", "pig pork farm oink bacon cute barn"],
      ["🐸", "Frog Face", "frog toad ribbit pond amphibian green hop"],
      ["🐵", "Monkey Face", "monkey primate banana ape cheeky jungle"],
      ["🐔", "Chicken", "bird poultry farm egg rooster hen cluck"],
      ["🐧", "Penguin", "bird ice antarctica waddle cute flightless tuxedo"],
      ["🐦", "Bird", "tweet chirp avian feather flyer sky robin"],
      ["🦆", "Duck", "mallard quack pond bird water waterfowl duckling"],
      ["🦅", "Eagle", "eagle bird freedom raptor predator armenia coat of arms"],
      ["🦉", "Owl", "owl wise nocturnal night bird smart study hoot"],
      ["🦇", "Bat", "bat night cave vampire spooky halloween wings"],
      ["🐺", "Wolf", "wolf pack howl wild animal predator moon"],
      ["🐴", "Horse Face", "horse pony stallion ride equine farm gallop"],
      ["🦄", "Unicorn", "magical fantasy horse horn colorful rainbow mythical"],
      ["🐝", "Honeybee", "bee honey sting pollinate buzz insect sweet"],
      ["🦋", "Butterfly", "flutter insect wings transformation beauty nature caterpillar"],
      ["🐢", "Turtle", "tortoise slow reptile shell ocean green calm"],
      ["🐍", "Snake", "serpent reptile venom hiss slither dangerous snake"],
      ["🐙", "Octopus", "tentacles ocean sea creature squid smart eight"],
      ["🐬", "Dolphin", "ocean mammal sea swimmer marine smart playful"],
      ["🐳", "Spouting Whale", "whale ocean blowhole water big giant leviathan"],
      ["🦈", "Shark", "shark predator ocean jaws sea teeth danger fin"],
      ["🌲", "Evergreen Tree", "pine tree forest nature winter wood evergreen"],
      ["🌳", "Deciduous Tree", "tree green park shade wood foliage oak"],
      ["🌴", "Palm Tree", "palm tropical beach island vacation coconut warm"],
      ["🌱", "Seedling", "plant sprout green growth new nature garden grow"],
      ["🌿", "Herb", "leaf green botanical medicinal plant cook mint"],
      ["🍀", "Four Leaf Clover", "lucky fortune lucky clover rare green st patrick"],
      ["💐", "Bouquet", "flowers gift romance celebration bloom flora petals"],
      ["🌸", "Cherry Blossom", "sakura bloom pink spring flower japan floral"],
      ["🌹", "Rose", "red flower romance love valentine flora smell thorn"],
      ["🌻", "Sunflower", "sunflower summer yellow seed tall bloom flora field"],
      ["🌞", "Sun with Face", "sunny sunshine warm bright morning day summer"],
      ["🌙", "Crescent Moon", "night sleepy moon dream sky bedtime sleep"],
      ["⭐", "Star", "yellow star favorite rating review shiny sky astral"],
      ["⚡", "High Voltage", "lightning electric storm flash energy thunder power bolt"],
      ["🌈", "Rainbow", "colorful pride rain weather spectrum hope sky colors"],
      ["☀️", "Sun", "sunny heat summer day bright weather warm ray"],
      ["🌧️", "Cloud with Rain", "rain downpour weather stormy drop wet drizzle"],
      ["❄️", "Snowflake", "snow cold winter frost freeze holiday chilly ice"],
    ],
    food: [
      ["🍏", "Green Apple", "apple fruit healthy fresh green sour granny"],
      ["🍎", "Red Apple", "apple fruit sweet healthy armenian orchard red"],
      ["🍐", "Pear", "pear fruit sweet orchard juicy green"],
      ["🍊", "Tangerine", "orange citrus fruit vitamin c juice peeled mandarin"],
      ["🍋", "Lemon", "citrus sour yellow lemonade tart juice zest"],
      ["🍌", "Banana", "banana potassium fruit yellow peel monkey snack ripe"],
      ["🍉", "Watermelon", "summer melon juicy sweet red slice fruit seeds"],
      ["🍇", "Grapes", "grape wine fruit purple green bunch armenia areni"],
      ["🍓", "Strawberry", "berry red sweet dessert fruit shortcake jam"],
      ["🍒", "Cherries", "cherry sweet red twin fruit orchard tart"],
      ["🍑", "Peach", "peach sweet fuzzy juicy fruit butt emoji apricot"],
      ["🍍", "Pineapple", "tropical fruit sweet spiky yellow hawaii ananas"],
      ["🥥", "Coconut", "tropical coconut milk island shell palm oil"],
      ["🥑", "Avocado", "guacamole toast keto healthy green fat butter seed"],
      ["🥦", "Broccoli", "vegetable green healthy fiber diet cabbage tree"],
      ["🌽", "Ear of Corn", "maize corn on the cob yellow grain farm popcorn sweet"],
      ["🌶️", "Hot Pepper", "chili spicy hot pepper red fire mexican seasoning jalapeño"],
      ["🥐", "Croissant", "french pastry butter bakery breakfast flaky bread cafe"],
      ["🍞", "Bread", "loaf bakery toast slice carb sandwich wheat lavash"],
      ["🥖", "Baguette Bread", "french bread bakery crusty loaf sandwich long"],
      ["🧀", "Cheese Wedge", "cheddar swiss dairy yellow mouse snack appetizer lori"],
      ["🥚", "Egg", "breakfast protein hen farm boil cook scramble shell"],
      ["🍳", "Cooking", "fried egg skillet breakfast pan cook fry sunny side"],
      ["🥞", "Pancakes", "flapjack syrup butter stack breakfast sweet brunch hotcakes"],
      ["🧇", "Waffle", "belgian waffle breakfast syrup butter grid sweet waffle"],
      ["🥓", "Bacon", "crispy pork breakfast strip sizzle meat savory breakfast"],
      ["🥩", "Cut of Meat", "steak beef butcher protein dinner cook grill khorovats"],
      ["🍗", "Poultry Leg", "chicken leg drumstick fried turkey thanksgiving meat wings"],
      ["🌭", "Hot Dog", "frankfurter sausage bun mustard stadium fast food wiener"],
      ["🍔", "Hamburger", "burger cheeseburger fast food beef bun grill diner patty"],
      ["🍟", "French Fries", "potato fries chips mcdonalds fast food salted crispy potato"],
      ["🍕", "Pizza", "slice cheese pepperoni delivery italian crust pie fast food pizza"],
      ["🥪", "Sandwich", "deli sub lunch ham bread slice cheese snack toast"],
      ["🌮", "Taco", "mexican shell beef salsa crunch dinner street food taco"],
      ["🌯", "Burrito", "wrap mexican beans rice cheese shawarma rolled shaurma"],
      ["🥗", "Green Salad", "healthy lettuce vegetable diet bowl dressing tossed greens"],
      ["🍝", "Spaghetti", "pasta noodles italian tomato sauce marinara dinner pasta"],
      ["🍜", "Steaming Bowl", "ramen noodles soup broth chopstick hot asian meal udon"],
      ["🍣", "Sushi", "nigiri raw fish salmon rice japanese seafood roll maki"],
      ["🍤", "Fried Shrimp", "tempura seafood prawn fried crispy snack shrimp"],
      ["🍦", "Soft Ice Cream", "vanilla swirl cone summer dessert frozen dairy softserve"],
      ["🍰", "Shortcake", "cake slice birthday pastry sweet dessert strawberry bakery cake"],
      ["🎂", "Birthday Cake", "candles party celebrate sweet dessert frosting anniversary cake"],
      ["🍩", "Doughnut", "donut sprinkles glaze sweet pastry bakery coffee snack doughnut"],
      ["🍪", "Cookie", "chocolate chip bakery sweet snack milk baked crunch cookie"],
      ["🍫", "Chocolate Bar", "cocoa sweet dessert candy treat dark milk square chocolate"],
      ["🍿", "Popcorn", "cinema movie cinema snack butter corn salt kernel cinema"],
      ["☕", "Hot Beverage", "coffee tea morning cafe espresso latte cappuccino brew armenian jazzve"],
      ["🧋", "Bubble Tea", "boba milk tea tapioca pearls straw sweet asian tapioca"],
      ["🍺", "Beer Mug", "beer ale lager pub drink alcohol cheers bar toast kilikia"],
      ["🍻", "Clinking Beer Mugs", "cheers celebrate drinks party pub toast bar beers"],
      ["🍷", "Wine Glass", "red wine vineyard armenia alcohol grape bar dinner areni"],
      ["🥂", "Clinking Glasses", "champagne celebration cheers toast wedding new year event bubbles"],
      ["🍸", "Cocktail Glass", "martini olive lounge bar alcohol drink classy party cocktail"],
    ],
    activities: [
      ["⚽", "Soccer Ball", "football soccer sport game world cup fifa kick match"],
      ["🏀", "Basketball", "nba hoop dribble sport court game dunk ball"],
      ["🏈", "American Football", "nfl touchdown gridiron pigskin super bowl helmet ball"],
      ["⚾", "Baseball", "mlb home run pitcher bat sport game strike ball"],
      ["🎾", "Tennis", "wimbledon racket court match serve grand slam sport ball"],
      ["🏐", "Volleyball", "beach spike net sport game summer sand ball"],
      ["🏓", "Ping Pong", "table tennis paddle ball paddle tournament sport pingpong"],
      ["🏸", "Badminton", "shuttlecock bird racket net sport gym shuttle"],
      ["🥊", "Boxing Glove", "boxing fight punch match combat champion sparring glove"],
      ["🥋", "Martial Arts Uniform", "karate judo taekwondo gi belt fight training uniform"],
      ["🛹", "Skateboard", "skate deck wheels halfpipe ollie street board skater"],
      ["🎿", "Skis", "skiing snow winter tsaghkadzor resort mountain alpine sport skis"],
      ["🏂", "Snowboard", "snowboard winter mountain powder shred halfpipe board"],
      ["🏋️", "Weight Lifter", "workout gym bench press fitness muscle dumbbell bodybuilder lift"],
      ["🏃", "Runner", "marathon jog sprint speed cardio cardio workout race running"],
      ["🧗", "Climber", "rock climbing bouldering mountain summit outdoor sport climb"],
      ["🏊", "Swimmer", "swimming pool water race stroke laps aquatic sport swim"],
      ["🚴", "Bicyclist", "bike cycling tour ride race cardio pedal workout cycling"],
      ["🏆", "Trophy", "champion first place win award victory cup gold tournament winner"],
      ["🥇", "1st Place Medal", "gold winner first champion prize medal number one gold"],
      ["🥈", "2nd Place Medal", "silver second place runner up medal prize silver"],
      ["🥉", "3rd Place Medal", "bronze third place medal prize honor bronze"],
      ["🎮", "Video Game", "gaming controller console playstation xbox nintendo gamer steam"],
      ["🎲", "Game Die", "dice board game tabletop roll luck casino random chance dice"],
      ["♟️", "Chess Pawn", "chess board strategy game checkmate king queen grandmaster armenia"],
      ["🎯", "Bullseye", "darts target archery goal hit accuracy direct win target"],
      ["🎳", "Bowling", "pins strike spare alley ball lane sport game bowling"],
      ["🎨", "Artist Palette", "painting art drawing colors canvas create design brush artist"],
      ["🎬", "Clapper Board", "cinema film movie directing take production hollywood video movie"],
      ["🎤", "Microphone", "karaoke sing music speech audio concert podcast stage mic"],
      ["🎧", "Headphone", "music audio podcast beats sound listen dj tracks headset"],
      ["🎸", "Guitar", "acoustic rock electric music strings instrument chord band guitar"],
      ["🎹", "Musical Keyboard", "piano synth instrument keys notes music song classical piano"],
    ],
    travel: [
      ["🚗", "Automobile", "car vehicle drive road sedan traffic travel commute auto"],
      ["🚕", "Taxi", "cab uber rideshare yellow airport city transport yandex gg"],
      ["🚙", "SUV", "car offroad jeep truck all wheel drive transport jeep"],
      ["🚌", "Bus", "city bus transit public transportation marshrutka school commuter bus"],
      ["🚎", "Trolleybus", "trolley transit electric city yerevan transport wire bus"],
      ["🏎️", "Racing Car", "f1 formula one speed race fast motorsport track racecar"],
      ["🚓", "Police Car", "cop police emergency siren 911 patrol law officer patrol"],
      ["🚑", "Ambulance", "emergency medical paramedics hospital siren rush rescue med"],
      ["🚒", "Fire Engine", "fire truck firefighter emergency siren ladder water fire"],
      ["🏍️", "Motorcycle", "motorbike bike chopper speed engine biker fast moto"],
      ["🛵", "Motor Scooter", "vespa moped delivery courier city scooter transport moped"],
      ["🚲", "Bicycle", "bike cycle eco commute pedal fitness city lane cycling"],
      ["🛴", "Kick Scooter", "electric scooter bird lime commute city sidewalk scooter"],
      ["🚨", "Police Car Light", "siren emergency flashing beacon alarm alert red blue siren"],
      ["🚇", "Metro", "subway underground train yerevan transit public commute station metro"],
      ["✈️", "Airplane", "plane flight travel airline vacation zvartnots airport sky flight"],
      ["🛫", "Airplane Departure", "takeoff flight leave airport trip vacation departure fly"],
      ["🛬", "Airplane Arrival", "landing flight arrive homecoming airport trip arrival land"],
      ["🚀", "Rocket", "space launch mission moon mars nasa astronaut takeoff speed rocket"],
      ["🛸", "Flying Saucer", "ufo alien spaceship invasion mystery sky saucer"],
      ["🚁", "Helicopter", "chopper rotor fly rescue pilot air transport heli"],
      ["🚢", "Ship", "cruise boat sea ferry ocean voyage transport vessel marine ship"],
      ["🗺️", "World Map", "travel geography atlas exploration trip locations world country map"],
      ["🗿", "Moai", "statue stone easter island ancient rock face meme stoneface"],
      ["🏰", "Castle", "fairytale royalty stone medieval fortress kingdom palace fortress"],
      ["🏟️", "Stadium", "arena sports concert match crowd fans field bleachers stadium"],
      ["🏕️", "Camping", "tent campfire outdoor wilderness nature hike sleep stars camp"],
      ["🏖️", "Beach with Umbrella", "beach sand ocean sea summer sunny vacation relax beach"],
      ["⛰️", "Mountain", "peak summit alps ararat hiking outdoor nature scenic landscape ararat"],
      ["🏠", "House", "home building residence roof residence dwelling domestic family house"],
      ["🏢", "Office Building", "corporate workplace skyscraper commercial work company agency office"],
      ["🏫", "School", "aua university academy college campus class education lecture uni"],
      ["⛪", "Church", "temple monastery chapel armenian church cross apostolic worship church"],
    ],
    objects: [
      ["💡", "Light Bulb", "idea bright smart creativity invent power energy think light idea"],
      ["📱", "Mobile Phone", "iphone android smartphone screen device call text app phone"],
      ["💻", "Laptop", "macbook computer study tech code programming homework online laptop"],
      ["⌨️", "Keyboard", "typing keys computer mechanical desktop code gamer type keyboard"],
      ["🖥️", "Desktop Computer", "monitor pc display workstation screen rig office computer"],
      ["📷", "Camera", "photo picture photography snapshot lens memory digital camera"],
      ["📸", "Camera with Flash", "photograph flash paparazzi capture image memory portrait photo"],
      ["🔍", "Magnifying Glass", "search find investigate research detect query inspect find"],
      ["🔬", "Microscope", "science lab biology chemistry experiment study lab research microscope"],
      ["🔋", "Battery", "power energy charge mobile electronics level full percent battery"],
      ["🔌", "Electric Plug", "socket wall outlet electricity charging adapter wire plug in cord"],
      ["💸", "Money with Wings", "cash spend fly away loss payment inflation broke transfer money"],
      ["💵", "Dollar Banknote", "money cash bill usd currency pay price profit funds dollar"],
      ["💰", "Money Bag", "wealth rich bag loot profit millionaire jackpot coins treasury bag"],
      ["💳", "Credit Card", "visa mastercard pay purchase debt bank tap contactless swipe card"],
      ["💎", "Gem Stone", "diamond jewel precious crystal expensive luxury treasure rare shine gem"],
      ["⚖️", "Balance Scale", "justice law court legal equality weighing scale fair attorney scale"],
      ["🔑", "Key", "unlock access door secret code password lock security key key"],
      ["🔒", "Locked", "secure safety private protection padlock safe encrypt guard lock"],
      ["🔓", "Unlocked", "open access free padlock security insecure breach unlock"],
      ["🔔", "Bell", "notification ring alert alarm reminder notice chime sound bell"],
      ["📦", "Package", "box delivery amazon shipping parcel post package order cardboard box"],
      ["✉️", "Envelope", "mail letter email message contact post address paper envelope"],
      ["📧", "Email", "inbox message electronic letter mail send correspondence notification email"],
      ["📝", "Memo", "pencil write note document draft paper list task homework plan memo"],
      ["📚", "Books", "study library reading textbook course syllabus knowledge learn exam books"],
      ["📖", "Open Book", "read story manual study literature learning pages book book"],
      ["📎", "Paperclip", "attach document clip file hold stationary office link clip"],
      ["📌", "Pushpin", "pin notice board sticky corkboard location attach mark tack pin"],
      ["📍", "Round Pushpin", "location map pin spot destination address placed here mark location"],
      ["🗑️", "Wastebasket", "trash delete bin discard garbage remove rubbish junk trash"],
    ],
    flags: [
      ["🇦🇲", "Armenia", "armenia yerevan hayastan flag tricolor red blue orange national homeland"],
      ["🇺🇸", "United States", "usa america stars stripes american flag united states"],
      ["🇫🇷", "France", "france french paris flag tricolor europe"],
      ["🇩🇪", "Germany", "germany deutschland berlin flag europe"],
      ["🇬🇧", "United Kingdom", "uk britain london union jack british flag"],
      ["🇪🇺", "European Union", "eu europe european union flag stars blue"],
      ["🇷🇺", "Russia", "russia moscow flag tricolor federation"],
      ["🇬🇪", "Georgia", "georgia tbilisi sakartvelo cross flag caucasian neighbor"],
      ["🇮🇷", "Iran", "iran tehran persia neighbor flag"],
      ["🇬🇷", "Greece", "greece athens hellas cross flag mediterranean"],
      ["🇮🇹", "Italy", "italy rome italia flag tricolor europe"],
      ["🇪🇸", "Spain", "spain madrid espana flag europe"],
      ["🇨🇦", "Canada", "canada ottawa maple leaf flag north america"],
      ["🇦🇺", "Australia", "australia canberra sydney southern cross oceania flag"],
      ["🇧🇷", "Brazil", "brazil brasilia south america green yellow flag"],
      ["🇯🇵", "Japan", "japan tokyo nippon rising sun flag asia"],
      ["🇰🇷", "South Korea", "korea seoul taegeukgi flag asia kpop"],
      ["🇨🇳", "China", "china beijing red stars flag asia"],
      ["🇮🇳", "India", "india new delhi tricolor chakra flag asia"],
      ["🏁", "Chequered Flag", "racing finish line formula one winner finish complete done flag"],
      ["🚩", "Triangular Flag", "red flag warning alert danger mark signal point flag"],
      ["🏳️‍🌈", "Rainbow Flag", "pride lgbtq rainbow freedom identity diversity love equality pride"],
    ],
  };

  /* --------------------------------------------------------------------------
     Emoji Picker Controller
     -------------------------------------------------------------------------- */

  let emojiPickerInitialized = false;
  const DEFAULT_QUICK_EMOJIS = ["🔥", "❤️", "😂", "🙏", "😭", "💀", "✨", "🎉", "💯", "👍", "🇦🇲", "🎓"];

  function getRecentQuickEmojis() {
    try {
      const raw = localStorage.getItem("aua_hub_recent_emojis");
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length > 0) {
          return parsed.slice(0, 12);
        }
      }
    } catch { }
    return DEFAULT_QUICK_EMOJIS;
  }

  function recordRecentEmoji(emoji) {
    if (!emoji) return;
    try {
      const list = getRecentQuickEmojis().filter((e) => e !== emoji);
      list.unshift(emoji);
      localStorage.setItem("aua_hub_recent_emojis", JSON.stringify(list.slice(0, 12)));
      renderEmojiQuickBar();
    } catch { }
  }

  function renderEmojiQuickBar() {
    const bar = document.getElementById("emojiQuickBar");
    if (!bar) return;
    const emojis = getRecentQuickEmojis();
    bar.innerHTML =
      `<span class="emoji-quick-label">⚡ Quick</span>` +
      emojis
        .map(
          (em) => `
        <button
          type="button"
          class="emoji-quick-btn"
          onclick="insertEmoji('${escapeHtml(em)}')"
          title="Insert ${escapeHtml(em)}"
        >${em}</button>
      `
        )
        .join("");
  }

  function initEmojiPicker() {
    if (emojiPickerInitialized) return;
    emojiPickerInitialized = true;
    renderEmojiQuickBar();
    renderEmojiCategoryTabs();
    renderEmojiGrid(state.activeEmojiCategory || "smileys", "");

    // Close picker on outside click
    document.addEventListener("click", (e) => {
      const picker = document.getElementById("chatEmojiPicker");
      const toggleBtn = document.getElementById("chatEmojiToggleBtn");
      if (!picker || picker.classList.contains("hidden")) return;
      if (!picker.contains(e.target) && !toggleBtn?.contains(e.target)) {
        closeEmojiPicker();
      }
    });

    // Close on Esc key
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeEmojiPicker();
        closeProfileAvatarViewer();
      }
    });

    // Reposition on window resize if open
    window.addEventListener("resize", () => {
      const picker = document.getElementById("chatEmojiPicker");
      if (picker && !picker.classList.contains("hidden")) {
        positionEmojiPicker();
      }
    });
  }

  function renderEmojiCategoryTabs() {
    const container = document.getElementById("emojiCategoryTabs");
    if (!container) return;
    container.innerHTML = EMOJI_CATEGORIES.map((cat) => `
    <button
      type="button"
      class="emoji-tab-btn ${cat.id === state.activeEmojiCategory ? "active" : ""}"
      onclick="switchEmojiCategory('${cat.id}')"
      title="${escapeHtml(cat.label)}"
    >${cat.icon}</button>
  `).join("");
  }

  function switchEmojiCategory(catId) {
    state.activeEmojiCategory = catId;
    const searchInput = document.getElementById("emojiSearchInput");
    if (searchInput) {
      searchInput.value = "";
      state.emojiSearchQuery = "";
      const clearBtn = document.getElementById("emojiClearSearchBtn");
      if (clearBtn) clearBtn.classList.add("hidden");
    }
    renderEmojiCategoryTabs();
    renderEmojiGrid(catId, "");
  }

  function renderEmojiGrid(catId, query = "") {
    const grid = document.getElementById("emojiGrid");
    if (!grid) return;

    const q = (query || "").trim().toLowerCase();
    let matches = [];

    if (q) {
      // Search across ALL categories
      for (const [category, items] of Object.entries(EMOJI_DATABASE)) {
        for (const item of items) {
          const [emoji, name, keywords] = item;
          if (name.toLowerCase().includes(q) || keywords.toLowerCase().includes(q) || emoji.includes(q)) {
            matches.push(item);
          }
        }
      }
    } else {
      matches = EMOJI_DATABASE[catId] || [];
    }

    if (matches.length === 0) {
      grid.innerHTML = `
      <div style="grid-column: 1 / -1; text-align:center; padding:36px 12px; color:var(--text-secondary); font-size:13px;">
        No emojis found for "${escapeHtml(query)}"
      </div>
    `;
      return;
    }

    grid.innerHTML = matches.map(([emoji, name, keywords]) => `
    <button
      type="button"
      class="emoji-item"
      title="${escapeHtml(name)}"
      data-emoji="${escapeHtml(emoji)}"
      data-name="${escapeHtml(name)}"
      onmouseenter="showEmojiFooterPreview('${escapeHtml(emoji)}', '${escapeHtml(name)}')"
      onclick="insertEmoji('${escapeHtml(emoji)}')"
    >${emoji}</button>
  `).join("");
  }

  function filterEmojis(val) {
    state.emojiSearchQuery = val;
    const clearBtn = document.getElementById("emojiClearSearchBtn");
    if (clearBtn) {
      clearBtn.classList.toggle("hidden", !val);
    }
    renderEmojiGrid(state.activeEmojiCategory, val);
  }

  function clearEmojiSearch() {
    const input = document.getElementById("emojiSearchInput");
    if (input) {
      input.value = "";
      input.focus();
    }
    filterEmojis("");
  }

  function showEmojiFooterPreview(emoji, name) {
    const sample = document.getElementById("emojiPreviewSample");
    const label = document.getElementById("emojiPreviewName");
    if (sample) sample.textContent = emoji;
    if (label) label.textContent = name;
  }

  function toggleEmojiPicker(e) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    initEmojiPicker();
    const picker = document.getElementById("chatEmojiPicker");
    if (!picker) return;
    const isOpen = !picker.classList.contains("hidden");
    if (isOpen) {
      closeEmojiPicker();
    } else {
      openEmojiPicker();
    }
  }

  function positionEmojiPicker() {
    const picker = document.getElementById("chatEmojiPicker");
    const btn = document.getElementById("chatEmojiToggleBtn");
    const container = document.querySelector(".spam-chat-container");
    if (!picker || !btn || !container) return;

    if (window.innerWidth <= 768) {
      picker.style.left = "";
      picker.style.right = "";
      picker.style.bottom = "";
      picker.style.top = "";
      picker.style.maxHeight = "";
      return;
    }

    const btnRect = btn.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const pickerWidth = picker.offsetWidth || 376;

    // Align picker's left edge near the emoji toggle button
    let left = btnRect.left - containerRect.left - 4;

    // Boundary checks: keep cleanly inside chat container
    const maxLeft = Math.max(12, containerRect.width - pickerWidth - 14);
    if (left > maxLeft) left = maxLeft;
    if (left < 14) left = 14;

    // Position vertically right above the emoji toggle button
    const bottom = Math.max(70, containerRect.bottom - btnRect.top + 10);
    const maxHeight = Math.max(280, Math.min(420, containerRect.height - bottom - 14));

    picker.style.left = `${Math.round(left)}px`;
    picker.style.right = "auto";
    picker.style.bottom = `${Math.round(bottom)}px`;
    picker.style.maxHeight = `${Math.round(maxHeight)}px`;
  }

  function openEmojiPicker() {
    initEmojiPicker();
    const picker = document.getElementById("chatEmojiPicker");
    const toggleBtn = document.getElementById("chatEmojiToggleBtn");
    if (!picker) return;
    renderEmojiQuickBar();
    picker.classList.remove("hidden");
    if (toggleBtn) toggleBtn.classList.add("active");
    positionEmojiPicker();
    const searchInput = document.getElementById("emojiSearchInput");
    if (searchInput && window.innerWidth > 768) {
      setTimeout(() => searchInput.focus(), 50);
    }
  }

  function closeEmojiPicker() {
    const picker = document.getElementById("chatEmojiPicker");
    const toggleBtn = document.getElementById("chatEmojiToggleBtn");
    if (picker) picker.classList.add("hidden");
    if (toggleBtn) toggleBtn.classList.remove("active");
  }

  function insertEmoji(emoji) {
    const input = document.getElementById("chatRichInput");
    if (!input) return;

    restoreChatInputSelection();

    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      const range = sel.getRangeAt(0);
      if (input.contains(range.commonAncestorContainer) || input === range.commonAncestorContainer) {
        range.deleteContents();
        const textNode = document.createTextNode(emoji);
        range.insertNode(textNode);
        range.setStartAfter(textNode);
        range.setEndAfter(textNode);
        sel.removeAllRanges();
        sel.addRange(range);
      } else {
        input.appendChild(document.createTextNode(emoji));
      }
    } else {
      input.appendChild(document.createTextNode(emoji));
    }

    recordRecentEmoji(emoji);

    // Trigger input event to update state and placeholder
    input.dispatchEvent(new Event("input", { bubbles: true }));
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  function backspaceChatEmoji() {
    const input = document.getElementById("chatRichInput");
    if (!input) return;
    restoreChatInputSelection();
    try {
      document.execCommand("delete", false, null);
    } catch {
      const text = input.textContent || "";
      if (text.length > 0) {
        const chars = Array.from(text);
        chars.pop();
        input.textContent = chars.join("");
      }
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  /* --------------------------------------------------------------------------
     Visual Rich Text Formatting (No Raw Tags in Typing Field)
     Robust Desktop & Mobile Formatting Logic with Real-time Active States
     -------------------------------------------------------------------------- */

  let savedChatSelectionRange = null;
  let lastFormatActionTimestamp = 0;

  function saveChatInputSelection() {
    const input = document.getElementById("chatRichInput");
    const sel = window.getSelection();
    if (!input || !sel || sel.rangeCount === 0) return;
    try {
      const range = sel.getRangeAt(0);
      if (input.contains(range.commonAncestorContainer) || input === range.commonAncestorContainer) {
        savedChatSelectionRange = range.cloneRange();
      }
    } catch { }
  }

  function restoreChatInputSelection() {
    const input = document.getElementById("chatRichInput");
    if (!input) return;
    input.focus();
    const sel = window.getSelection();
    if (!sel) return;

    if (savedChatSelectionRange) {
      try {
        sel.removeAllRanges();
        sel.addRange(savedChatSelectionRange);
        return;
      } catch { }
    }

    // Fallback: place caret at the end of input
    try {
      const range = document.createRange();
      range.selectNodeContents(input);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
      savedChatSelectionRange = range.cloneRange();
    } catch { }
  }

  function getClosestAncestorTag(node, tagName, stopElement) {
    let curr = node;
    const target = tagName.toLowerCase();
    while (curr && curr !== stopElement && curr !== document.body) {
      if (curr.nodeType === Node.ELEMENT_NODE && curr.tagName.toLowerCase() === target) {
        return curr;
      }
      curr = curr.parentNode;
    }
    return null;
  }

  function isCaretAtEndOfElement(sel, element) {
    if (!sel || sel.rangeCount === 0 || !element) return false;
    const range = sel.getRangeAt(0);
    if (!range.collapsed) return false;

    try {
      const endRange = document.createRange();
      endRange.selectNodeContents(element);
      endRange.setStart(range.endContainer, range.endOffset);
      const trailingText = endRange.toString().replace(/\u200B/g, "");
      return trailingText.length === 0;
    } catch {
      return false;
    }
  }

  function isFormatActiveAtCaret(cmd) {
    const input = document.getElementById("chatRichInput");
    if (!input) return false;

    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return false;
    const range = sel.getRangeAt(0);
    if (!input.contains(range.commonAncestorContainer) && input !== range.commonAncestorContainer) {
      return false;
    }

    if (cmd === "code") {
      return Boolean(getClosestAncestorTag(sel.anchorNode, "code", input));
    }

    try {
      return Boolean(document.queryCommandState(cmd));
    } catch {
      return false;
    }
  }

  function updateFormatToolbarActiveStates() {
    const input = document.getElementById("chatRichInput");
    if (!input) return;

    const btnBold = document.getElementById("formatBtnBold");
    const btnItalic = document.getElementById("formatBtnItalic");
    const btnUnderline = document.getElementById("formatBtnUnderline");
    const btnStrike = document.getElementById("formatBtnStrike");
    const btnCode = document.getElementById("formatBtnCode");

    const boldActive = isFormatActiveAtCaret("bold");
    const italicActive = isFormatActiveAtCaret("italic");
    const underlineActive = isFormatActiveAtCaret("underline");
    const strikeActive = isFormatActiveAtCaret("strikeThrough");
    const codeActive = isFormatActiveAtCaret("code");

    if (btnBold) {
      btnBold.classList.toggle("active", boldActive);
      btnBold.setAttribute("aria-pressed", boldActive ? "true" : "false");
    }
    if (btnItalic) {
      btnItalic.classList.toggle("active", italicActive);
      btnItalic.setAttribute("aria-pressed", italicActive ? "true" : "false");
    }
    if (btnUnderline) {
      btnUnderline.classList.toggle("active", underlineActive);
      btnUnderline.setAttribute("aria-pressed", underlineActive ? "true" : "false");
    }
    if (btnStrike) {
      btnStrike.classList.toggle("active", strikeActive);
      btnStrike.setAttribute("aria-pressed", strikeActive ? "true" : "false");
    }
    if (btnCode) {
      btnCode.classList.toggle("active", codeActive);
      btnCode.setAttribute("aria-pressed", codeActive ? "true" : "false");
    }
  }

  function toggleCodeFormat(input, sel, range, isCollapsed) {
    const existingCode = getClosestAncestorTag(sel.anchorNode, "code", input);
    if (!isCollapsed) {
      // Text IS selected: toggle code tag
      if (existingCode) {
        const parent = existingCode.parentNode;
        while (existingCode.firstChild) {
          parent.insertBefore(existingCode.firstChild, existingCode);
        }
        parent.removeChild(existingCode);
      } else {
        const selectedText = range.toString();
        if (selectedText) {
          const fragment = range.extractContents();
          const codeEl = document.createElement("code");
          codeEl.appendChild(fragment);
          range.insertNode(codeEl);
          range.selectNodeContents(codeEl);
          sel.removeAllRanges();
          sel.addRange(range);
        }
      }
    } else {
      // NO text selected (caret collapsed)
      if (existingCode) {
        // Exit code mode
        if (existingCode.textContent.replace(/\u200B/g, "") === "") {
          const parent = existingCode.parentNode;
          const zwsp = document.createTextNode("\u200B");
          parent.replaceChild(zwsp, existingCode);
          range.setStart(zwsp, 1);
          range.collapse(true);
        } else {
          const zwsp = document.createTextNode("\u200B");
          if (existingCode.nextSibling) {
            existingCode.parentNode.insertBefore(zwsp, existingCode.nextSibling);
          } else {
            existingCode.parentNode.appendChild(zwsp);
          }
          range.setStart(zwsp, 1);
          range.collapse(true);
        }
        sel.removeAllRanges();
        sel.addRange(range);
      } else {
        // Enter code mode: insert <code>\u200B</code> and put caret inside
        const codeEl = document.createElement("code");
        const zwsp = document.createTextNode("\u200B");
        codeEl.appendChild(zwsp);
        range.insertNode(codeEl);
        range.setStart(zwsp, 1);
        range.collapse(true);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }
  }

  function toggleChatFormat(cmd) {
    const input = document.getElementById("chatRichInput");
    if (!input) return;

    if (document.activeElement !== input) {
      restoreChatInputSelection();
    }

    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) {
      input.focus();
      return;
    }

    const range = sel.getRangeAt(0);
    const isCollapsed = range.isCollapsed;

    if (cmd === "code") {
      toggleCodeFormat(input, sel, range, isCollapsed);
    } else {
      const currentlyActive = isFormatActiveAtCaret(cmd);

      // Apply native execCommand formatting
      document.execCommand(cmd, false, null);

      // If caret was collapsed and we just deactivated an active format,
      // and caret is at the end of that tag, ensure caret is physically positioned
      // outside the tag so mobile keyboards (e.g. Gboard) cannot append into the styled text node.
      if (isCollapsed && currentlyActive) {
        const tagMap = { bold: "b", italic: "i", underline: "u", strikeThrough: "s" };
        const tagName = tagMap[cmd];
        if (tagName) {
          const tagEl = getClosestAncestorTag(sel.anchorNode, tagName, input);
          if (tagEl && isCaretAtEndOfElement(sel, tagEl)) {
            const zwsp = document.createTextNode("\u200B");
            if (tagEl.nextSibling) {
              tagEl.parentNode.insertBefore(zwsp, tagEl.nextSibling);
            } else {
              tagEl.parentNode.appendChild(zwsp);
            }
            const newRange = document.createRange();
            newRange.setStart(zwsp, 1);
            newRange.collapse(true);
            sel.removeAllRanges();
            sel.addRange(newRange);
          }
        }
      }
    }

    saveChatInputSelection();
    updateFormatToolbarActiveStates();
    input.focus();
  }

  function formatSelection(cmd) {
    toggleChatFormat(cmd);
  }

  function handleFormatPointerDown(e, cmd) {
    if (e) {
      e.preventDefault();
    }
    if (Date.now() - lastFormatActionTimestamp < 200) {
      return;
    }
    lastFormatActionTimestamp = Date.now();
    toggleChatFormat(cmd);
  }

  function handleFormatButtonClick(e, cmd) {
    if (e) {
      e.preventDefault();
    }
    if (Date.now() - lastFormatActionTimestamp < 400) {
      return;
    }
    lastFormatActionTimestamp = Date.now();
    toggleChatFormat(cmd);
  }

  function handleChatKeydown(e) {
    // Enter sends message, Shift+Enter inserts newline
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendSpamChatMessage(e);
      return;
    }

    // Visual shortcuts: Ctrl+B / Cmd+B for bold, Ctrl+I for italic, Ctrl+U for underline, Ctrl+Shift+X for strike, Ctrl+K for code
    if (e.ctrlKey || e.metaKey) {
      const k = e.key.toLowerCase();
      if (k === "b") {
        e.preventDefault();
        toggleChatFormat("bold");
      } else if (k === "i") {
        e.preventDefault();
        toggleChatFormat("italic");
      } else if (k === "u") {
        e.preventDefault();
        toggleChatFormat("underline");
      } else if (k === "x" && e.shiftKey) {
        e.preventDefault();
        toggleChatFormat("strikeThrough");
      } else if (k === "k" || (k === "e" && e.shiftKey)) {
        e.preventDefault();
        toggleChatFormat("code");
      }
    }
  }

  function handleChatKeyup(e) {
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  function handleChatMouseup(e) {
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  function handleChatFocus(e) {
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  function handleChatInput(e) {
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  function handleChatPaste(e) {
    // Prevent hostile or bloated styled markup from external apps (e.g. Word, Google Docs)
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData("text/plain");
    document.execCommand("insertText", false, text);
    saveChatInputSelection();
    updateFormatToolbarActiveStates();
  }

  // Global selectionchange listener to keep toolbar buttons in sync as caret moves
  document.addEventListener("selectionchange", () => {
    const input = document.getElementById("chatRichInput");
    if (!input) return;
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      const range = sel.getRangeAt(0);
      if (input.contains(range.commonAncestorContainer) || input === range.commonAncestorContainer) {
        savedChatSelectionRange = range.cloneRange();
        updateFormatToolbarActiveStates();
      }
    }
  });

  /* --------------------------------------------------------------------------
     Media Uploading & Attachments (Images, GIFs, Videos)
     -------------------------------------------------------------------------- */

  async function handleChatMediaUpload(inputEl) {
    const file = inputEl.files?.[0];
    if (!file) return;

    if (!state.user) {
      showToast("Please sign in to upload media to Chat.", true);
      openAuthModal("login");
      inputEl.value = "";
      return;
    }

    const isVideo = file.type.startsWith("video/") || /\.(mp4|webm|mov)$/i.test(file.name);
    const maxBytes = isVideo ? 25 * 1024 * 1024 : 10 * 1024 * 1024;
    const maxLabel = isVideo ? "25 MB" : "10 MB";

    if (file.size > maxBytes) {
      showToast(`File size (${(file.size / (1024 * 1024)).toFixed(1)} MB) exceeds the ${maxLabel} limit.`, true);
      inputEl.value = "";
      return;
    }

    const strip = document.getElementById("chatPendingMediaStrip");
    const previewContent = document.getElementById("chatMediaPreviewContent");
    if (strip && previewContent) {
      strip.classList.remove("hidden");
      previewContent.innerHTML = `<span style="opacity:0.85;">⏳ Uploading ${escapeHtml(file.name)} (${(file.size / (1024 * 1024)).toFixed(1)} MB)...</span>`;
    }

    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await apiFetch("/api/upload", { method: "POST", body: fd });

      const mediaType = res.media_type || (isVideo ? "video" : (file.type === "image/gif" || /\.gif$/i.test(file.name) ? "gif" : "image"));
      state.chatPendingMediaUrl = res.url;
      state.chatPendingMediaType = mediaType;
      state.chatPendingMediaName = file.name;
      state.chatPendingImageUrl = res.url; // legacy compatibility

      renderPendingMediaPreview(file, res.url, mediaType);
      showToast(`Media attached (${mediaType.toUpperCase()})`);
    } catch (err) {
      clearChatMediaAttachment();
      showToast(err.message || "Failed to upload file.", true);
    } finally {
      inputEl.value = "";
    }
  }

  function renderPendingMediaPreview(file, url, mediaType) {
    const strip = document.getElementById("chatPendingMediaStrip");
    const previewContent = document.getElementById("chatMediaPreviewContent");
    if (!strip || !previewContent) return;

    strip.classList.remove("hidden");
    const fileName = file ? file.name : "Attachment";
    const fileSizeMb = file ? (file.size / (1024 * 1024)).toFixed(1) + " MB" : "";

    if (mediaType === "video") {
      previewContent.innerHTML = `
      <span style="font-size:22px; line-height:1;">🎬</span>
      <div style="overflow:hidden;">
        <div style="font-weight:700; color:var(--text-white); max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
          ${escapeHtml(fileName)}
        </div>
        <div style="font-size:11px; color:var(--neon-blue-bright);">Video &bull; ${fileSizeMb || "Ready to send"}</div>
      </div>
    `;
    } else {
      previewContent.innerHTML = `
      <img src="${escapeHtml(url)}" class="chat-media-preview-thumb" alt="Preview" />
      <div style="overflow:hidden;">
        <div style="font-weight:700; color:var(--text-white); max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
          ${escapeHtml(fileName)}
        </div>
        <div style="font-size:11px; color:var(--neon-blue-bright);">${mediaType === "gif" ? "Animated GIF" : "Image"} &bull; ${fileSizeMb || "Ready to send"}</div>
      </div>
    `;
    }
  }

  function clearChatMediaAttachment() {
    state.chatPendingMediaUrl = "";
    state.chatPendingMediaType = "";
    state.chatPendingMediaName = "";
    state.chatPendingImageUrl = "";
    const strip = document.getElementById("chatPendingMediaStrip");
    if (strip) strip.classList.add("hidden");
  }

  /* --------------------------------------------------------------------------
     Chat Message Stream & WebSocket
     -------------------------------------------------------------------------- */

  async function loadSpamChatMessages(markRead = false) {
    const stream = document.getElementById("spamMessagesStream");
    if (!stream) return;

    try {
      const data = await apiFetch("/api/spam-chat/messages");
      const msgs = data.messages || [];
      renderSpamChatStream(msgs);

      if (markRead) {
        const latestId = msgs.length > 0 ? msgs[msgs.length - 1].id : 0;
        updateUnreadBadge(0);
        if (state.user && latestId > 0) {
          apiFetch("/api/spam-chat/read", {
            method: "POST",
            body: JSON.stringify({ latest_message_id: latestId }),
          }).catch(() => { });
        }
      } else {
        updateUnreadBadge(data.unread_count || 0);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderSpamChatStream(messages) {
    const stream = document.getElementById("spamMessagesStream");
    if (!stream) return;

    state.chatMessagesMap = new Map();
    if (messages.length === 0) {
      stream.innerHTML = `
      <div style="text-align:center; padding:60px 20px; color:var(--text-secondary);">
        <div style="font-size:38px; margin-bottom:8px;">💬</div>
        <h3 style="color:var(--text-white); font-size:18px; font-weight:800;">Welcome to Campus Chat!</h3>
        <p style="font-size:14px; margin-top:6px; max-width:440px; margin-left:auto; margin-right:auto;">
          Post announcements, lost &amp; found items, advertisements, events, or chat with campus peers.
        </p>
      </div>
    `;
      return;
    }

    stream.innerHTML = messages.map((m) => buildSpamMessageHtml(m)).join("");
    stream.querySelectorAll(".chat-bubble-row").forEach((row) => {
      const msgId = Number(row.dataset.msgId);
      if (msgId) {
        initChatTouchGestures(row, msgId);
        initDesktopHoverPicker(row);
      }
    });
    scrollChatToBottom(false);
  }

  const CHAT_BLUE_PALETTE = [
    {
      name: "sky",
      primary: "#38BDF8", // Bright Sky Blue
      subtle: "rgba(56, 189, 248, 0.15)",
      border: "rgba(56, 189, 248, 0.45)",
      glow: "rgba(56, 189, 248, 0.22)",
    },
    {
      name: "cyan",
      primary: "#22D3EE", // Electric Cyan
      subtle: "rgba(34, 211, 238, 0.15)",
      border: "rgba(34, 211, 238, 0.45)",
      glow: "rgba(34, 211, 238, 0.22)",
    },
    {
      name: "ice",
      primary: "#7DD3FC", // Ice Frost Blue
      subtle: "rgba(125, 211, 252, 0.15)",
      border: "rgba(125, 211, 252, 0.45)",
      glow: "rgba(125, 211, 252, 0.20)",
    },
    {
      name: "cobalt",
      primary: "#60A5FA", // Vibrant Cobalt
      subtle: "rgba(96, 165, 250, 0.15)",
      border: "rgba(96, 165, 250, 0.45)",
      glow: "rgba(96, 165, 250, 0.22)",
    },
    {
      name: "indigo",
      primary: "#818CF8", // Indigo Periwinkle
      subtle: "rgba(129, 140, 248, 0.15)",
      border: "rgba(129, 140, 248, 0.45)",
      glow: "rgba(129, 140, 248, 0.22)",
    },
    {
      name: "aqua",
      primary: "#2DD4BF", // Aqua Teal
      subtle: "rgba(45, 212, 191, 0.15)",
      border: "rgba(45, 212, 191, 0.45)",
      glow: "rgba(45, 212, 191, 0.22)",
    },
    {
      name: "sapphire",
      primary: "#3B82F6", // Sapphire Electric
      subtle: "rgba(59, 130, 246, 0.15)",
      border: "rgba(59, 130, 246, 0.48)",
      glow: "rgba(59, 130, 246, 0.24)",
    },
    {
      name: "azure",
      primary: "#93C5FD", // Bright Azure
      subtle: "rgba(147, 197, 253, 0.15)",
      border: "rgba(147, 197, 253, 0.45)",
      glow: "rgba(147, 197, 253, 0.20)",
    },
    {
      name: "ocean",
      primary: "#0EA5E9", // Deep Ocean Sky
      subtle: "rgba(14, 165, 233, 0.15)",
      border: "rgba(14, 165, 233, 0.48)",
      glow: "rgba(14, 165, 233, 0.24)",
    },
    {
      name: "steel",
      primary: "#A5B4FC", // Steel Periwinkle
      subtle: "rgba(165, 180, 252, 0.15)",
      border: "rgba(165, 180, 252, 0.45)",
      glow: "rgba(165, 180, 252, 0.20)",
    },
    {
      name: "crystal",
      primary: "#67E8F9", // Crystal Cyan
      subtle: "rgba(103, 232, 249, 0.15)",
      border: "rgba(103, 232, 249, 0.45)",
      glow: "rgba(103, 232, 249, 0.22)",
    },
    {
      name: "electric",
      primary: "#6366F1", // Electric Indigo
      subtle: "rgba(99, 102, 241, 0.15)",
      border: "rgba(99, 102, 241, 0.48)",
      glow: "rgba(99, 102, 241, 0.24)",
    },
  ];

  function getChatUserColor(userId, userName = "") {
    let seed = 0;
    if (userId) {
      const idNum = Number(userId) || 0;
      seed = (idNum * 2654435761) >>> 0;
    } else if (userName) {
      for (let i = 0; i < userName.length; i++) {
        seed = (seed << 5) - seed + userName.charCodeAt(i);
        seed |= 0;
      }
      seed = (seed * 2654435761) >>> 0;
    }
    const idx = Math.abs(seed) % CHAT_BLUE_PALETTE.length;
    return CHAT_BLUE_PALETTE[idx];
  }

  const TELEGRAM_REACTIONS = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾",
    "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "💻", "👀", "🎃", "🙈", "😇",
    "😨", "🤝", "✍️", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿", "🆒",
    "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷", "🤦", "🙋", "😡"
  ];

  const TELEGRAM_QUICK_REACTIONS = ["👍", "👎", "❤️", "🔥", "🥰", "👏", "😁"];

  const EMOJI_KEYWORDS = {
    "👍": "thumbs up like good ok yes",
    "👎": "thumbs down dislike bad no",
    "❤": "heart love red like",
    "❤️": "heart love red like",
    "🔥": "fire flame lit hot",
    "🥰": "smile love hearts adore",
    "👏": "clap applause bravo",
    "😁": "grin smile happy laugh",
    "🤔": "think hmm wonder ponder",
    "🤯": "mind blown explode shock",
    "😱": "scream gasp scared fear",
    "🤬": "curse swear angry mad",
    "😢": "cry tear sad",
    "🎉": "party celebration celebrate tada",
    "🤩": "star eyes excited wow",
    "🤮": "vomit puke sick gross",
    "💩": "poop poo shit crap",
    "🙏": "pray please thanks pray",
    "👌": "ok okay perfect nice",
    "🕊": "dove peace bird",
    "🤡": "clown fool joke",
    "🥱": "yawn tired sleepy",
    "🥴": "woozy drunk dizzy",
    "😍": "heart eyes love in love",
    "🐳": "whale ocean blue",
    "🌚": "moon face dark creepy",
    "🌭": "hotdog food sausage",
    "💯": "hundred 100 percent perfect real",
    "🤣": "rofl laugh rolling funny",
    "⚡": "lightning bolt electric zap flash",
    "🍌": "banana fruit yellow",
    "🏆": "trophy winner award cup",
    "💔": "broken heart heartbreak sad",
    "🤨": "raised eyebrow skeptical doubt",
    "😐": "neutral face meh straight",
    "🍓": "strawberry fruit red berry",
    "🍾": "champagne bottle drink celebrate party",
    "💋": "kiss lips romantic red",
    "🖕": "middle finger rude",
    "😈": "devil imp evil naughty smile",
    "😴": "sleep sleeping zzz tired",
    "😭": "sob crying loudly tears sad",
    "🤓": "nerd geek glasses smart",
    "👻": "ghost spooky halloween boo",
    "💻": "laptop computer coder dev tech programmer",
    "👀": "eyes look watching peeking",
    "🎃": "pumpkin jackolantern halloween",
    "🙈": "see no evil monkey shy hide",
    "😇": "angel halo innocent saint",
    "😨": "fear scared worried anxious",
    "🤝": "handshake deal agreement shake",
    "✍": "write writing pen author notes",
    "✍️": "write writing pen author notes",
    "🤗": "hug hugging embrace warm",
    "🫡": "salute yes sir respect honor",
    "🎅": "santa claus christmas holiday",
    "🎄": "christmas tree holiday pine",
    "☃": "snowman winter snow cold",
    "☃️": "snowman winter snow cold",
    "💅": "nail polish slay sassy beauty",
    "🤪": "crazy zany goofy wild silly",
    "🗿": "moai easter island stone statue sigma",
    "🆒": "cool text badge blue",
    "💘": "cupid heart arrow love valentine",
    "🙉": "hear no evil monkey deaf ears",
    "🦄": "unicorn magic magical horse",
    "😘": "kiss blow kiss love flirt",
    "💊": "pill medicine capsule drug",
    "🙊": "speak no evil monkey quiet hush",
    "😎": "cool sunglasses shades chill",
    "👾": "alien monster retro space game 8bit",
    "🤷": "shrug person whatever idk who knows",
    "🤦": "facepalm duh sigh annoyed upset",
    "🙋": "raising hand pick me volunteer question here",
    "😡": "pout angry red mad rage"
  };

  function formatReactionTimestamp(dateInput) {
    if (!dateInput) return "Recently";
    const d = new Date(dateInput);
    if (isNaN(d.getTime())) return "Recently";
    const now = new Date();
    const timeStr = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) {
      return `Today at ${timeStr}`;
    }
    const yesterday = new Date();
    yesterday.setDate(now.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) {
      return `Yesterday at ${timeStr}`;
    }
    const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const m = monthNames[d.getMonth()];
    return `${m} ${d.getDate()} at ${timeStr}`;
  }

  function makeHorizontalScrollable(el) {
    if (!el || el.dataset.hScrollInit) return;
    el.dataset.hScrollInit = "1";
    el.addEventListener("wheel", (e) => {
      if (e.deltaY !== 0) {
        e.preventDefault();
        el.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  }

  function formatRelativeTime(dateInput) {
    if (!dateInput) return "";
    const d = new Date(dateInput);
    if (isNaN(d.getTime())) return "";
    const now = new Date();
    const diffSec = Math.floor((now - d) / 1000);
    if (diffSec < 45) return "just now";
    if (diffSec < 3600) {
      const mins = Math.max(1, Math.floor(diffSec / 60));
      return `${mins}m ago`;
    }
    if (diffSec < 86400) {
      const hours = Math.floor(diffSec / 3600);
      return `${hours}h ago`;
    }
    const days = Math.floor(diffSec / 86400);
    if (days === 1) {
      const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      return `yesterday at ${timeStr}`;
    }
    if (days < 7) {
      return `${days}d ago`;
    }
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function normalizeReactionUser(u) {
    if (!u) return { id: 0, name: "AUA Student", avatar: "", created_at: null };
    if (typeof u === "string") {
      return { id: 0, name: u, avatar: "", created_at: null };
    }
    return {
      id: Number(u.id) || 0,
      name: u.name || "AUA Student",
      avatar: u.avatar || u.avatar_url || "",
      created_at: u.created_at || null,
    };
  }

  function renderReactionsHtml(msgId, reactions) {
    if (!reactions || reactions.length === 0) return "";
    const currentUserId = Number(state.user?.id) || 0;
    return reactions
      .filter((r) => r.count > 0)
      .map((r) => {
        const isActive = Boolean(currentUserId && (r.user_ids || []).includes(currentUserId));
        const userObjects = (r.users || []).map(normalizeReactionUser);

        // Tooltip showing who reacted and when
        const tooltipItems = userObjects.map((u) => {
          const timeStr = u.created_at ? ` • ${formatRelativeTime(u.created_at)}` : "";
          return `${u.name}${timeStr}`;
        });
        const tooltip = tooltipItems.slice(0, 8).join(", ") + (tooltipItems.length > 8 ? "…" : "");

        // Compact micro avatars on right side of reaction chip (hidden when many reactions > 3)
        let avatarsHtml = "";
        if (r.count <= 3 && userObjects.length > 0) {
          avatarsHtml = `
          <div class="reaction-micro-avatar-stack">
            ${userObjects.slice(0, 3).map((u) => {
            const initial = getUserInitial(u.name);
            if (u.avatar) {
              return `<img src="${escapeHtml(u.avatar)}" class="reaction-micro-avatar" alt="${escapeHtml(u.name)}" title="${escapeHtml(u.name)} (${formatRelativeTime(u.created_at)})" onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='inline-flex';" /><span class="reaction-micro-avatar fallback" style="display:none;" title="${escapeHtml(u.name)} (${formatRelativeTime(u.created_at)})">${escapeHtml(initial)}</span>`;
            }
            return `<span class="reaction-micro-avatar fallback" title="${escapeHtml(u.name)} (${formatRelativeTime(u.created_at)})">${escapeHtml(initial)}</span>`;
          }).join("")}
          </div>
        `;
        }

        return `
        <button
          type="button"
          class="chat-reaction-badge ${isActive ? "active" : ""}"
          onclick="toggleMessageReaction(${msgId}, '${escapeHtml(r.emoji)}')"
          title="${escapeHtml(tooltip || r.emoji)}"
        >
          <span class="chat-reaction-emoji">${r.emoji}</span>
          <span class="chat-reaction-count">${r.count}</span>
          ${avatarsHtml}
        </button>
      `;
      })
      .join("");
  }

  function updateMessageReactionsDOM(msgId, reactions) {
    const idNum = Number(msgId);
    const wrapper = document.getElementById(`chatReactions-${idNum}`);
    if (wrapper) {
      wrapper.innerHTML = renderReactionsHtml(idNum, reactions);
    }
    const cached = state.chatMessagesMap.get(idNum);
    if (cached) {
      cached.reactions = reactions;
    }
    if (state.activeMenuMsgId === idNum) {
      updateTelegramMenuReactionsRow(reactions);
    }
    if (state.viewingReactionsMsgId === idNum) {
      renderMessageReactionsModalContent(idNum, reactions, state.viewingReactionsFilterEmoji);
    }
  }

  async function toggleMessageReaction(msgId, emoji) {
    if (!state.user) {
      showToast("Please sign in to react to messages.", true);
      openAuthModal("login");
      return;
    }

    // Dismiss open pickers
    document.querySelectorAll(".chat-reaction-picker.visible, .chat-reaction-picker.desktop-active").forEach((el) => {
      el.classList.remove("visible");
      el.classList.remove("desktop-active");
    });

    try {
      const res = await apiFetch(`/api/spam-chat/messages/${msgId}/react`, {
        method: "POST",
        body: JSON.stringify({ emoji: emoji }),
      });
      if (res && res.reactions) {
        updateMessageReactionsDOM(msgId, res.reactions);
      }
    } catch (err) {
      showToast(err.message || "Failed to update reaction.", true);
    }
  }

  function spawnHeartParticle(x, y) {
    const heart = document.createElement("div");
    heart.className = "floating-heart-particle";
    heart.textContent = "❤️";
    heart.style.left = `${x}px`;
    heart.style.top = `${y}px`;
    document.body.appendChild(heart);
    setTimeout(() => {
      heart.remove();
    }, 850);
  }

  function setChatReply(msgId) {
    if (!state.user) {
      showToast("Please sign in to reply to messages.", true);
      openAuthModal("login");
      return;
    }
    const idNum = Number(msgId);
    const msg = state.chatMessagesMap.get(idNum);
    if (!msg) return;

    const isOwn = state.user && msg.web_user_id === state.user.id;
    const authorName = msg.author_name || (isOwn ? "You" : "AUA Student");
    let snippet = "";
    if (msg.content_html) {
      const tmp = document.createElement("div");
      tmp.innerHTML = msg.content_html;
      snippet = tmp.textContent.trim().replace(/\s+/g, " ");
    }
    if (!snippet && msg.image_url) {
      snippet = (msg.media_type === "video") ? "📹 Video" : "📷 Photo";
    }
    if (!snippet) snippet = "Message";
    if (snippet.length > 65) snippet = snippet.slice(0, 65) + "…";

    state.chatReplyTo = {
      id: idNum,
      authorName: authorName,
      snippet: snippet,
    };

    const previewStrip = document.getElementById("chatReplyPreviewStrip");
    const authorEl = document.getElementById("chatReplyAuthor");
    const snippetEl = document.getElementById("chatReplySnippet");

    if (previewStrip && authorEl && snippetEl) {
      authorEl.textContent = authorName;
      snippetEl.textContent = snippet;
      previewStrip.classList.remove("hidden");
      previewStrip.style.removeProperty("display");
    }

    const input = document.getElementById("chatRichInput");
    if (input) {
      input.focus();
    }
  }

  function cancelChatReply(e) {
    if (e) {
      if (typeof e.preventDefault === "function") e.preventDefault();
      if (typeof e.stopPropagation === "function") e.stopPropagation();
    }
    state.chatReplyTo = null;
    const previewStrip = document.getElementById("chatReplyPreviewStrip");
    if (previewStrip) {
      previewStrip.classList.add("hidden");
      previewStrip.style.setProperty("display", "none", "important");
    }
  }

  function scrollToMessage(msgId) {
    const el = document.querySelector(`.chat-bubble-row[data-msg-id="${msgId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("message-highlight-pulse");
      void el.offsetWidth;
      el.classList.add("message-highlight-pulse");
      setTimeout(() => {
        el.classList.remove("message-highlight-pulse");
      }, 1800);
    } else {
      showToast("Message is not in recent history.", false);
    }
  }

  function handleMessageDblClick(e, msgId) {
    if (e && e.target && e.target.closest("a, button, .chat-quote-block, .chat-reaction-btn, .chat-reaction-badge, input, textarea")) {
      return;
    }
    if (e && e.preventDefault) e.preventDefault();

    const rowEl = document.querySelector(`.chat-bubble-row[data-msg-id="${msgId}"]`);
    // If touch double-tap already handled this reaction within the last 600ms, ignore
    if (rowEl && rowEl._lastDoubleTapReaction && (Date.now() - rowEl._lastDoubleTapReaction < 600)) {
      return;
    }

    const rect = rowEl ? rowEl.getBoundingClientRect() : null;
    const clientX = (e && typeof e.clientX === "number" && e.clientX > 0)
      ? e.clientX
      : (rect ? rect.left + rect.width / 2 : window.innerWidth / 2);
    const clientY = (e && typeof e.clientY === "number" && e.clientY > 0)
      ? e.clientY
      : (rect ? rect.top + rect.height / 2 : window.innerHeight / 2);

    if (navigator.vibrate) navigator.vibrate([30, 20]);
    spawnHeartParticle(clientX, clientY);
    toggleMessageReaction(msgId, "❤️");
  }

  function handleMessageContextMenu(e, msgId) {
    if (e.target.closest("a, button, input, textarea")) return;
    e.preventDefault();
    openTelegramMessageMenu(msgId, e.clientX, e.clientY);
  }

  let flyoutCloseTimer = null;

  function getMessageReactorsList(msgId) {
    const msg = state.chatMessagesMap.get(Number(msgId));
    if (!msg) return [];
    const reactions = msg.reactions || [];
    const allEntries = [];
    reactions.forEach((r) => {
      (r.users || []).forEach((u) => {
        const norm = normalizeReactionUser(u);
        allEntries.push({
          ...norm,
          emoji: r.emoji,
        });
      });
    });
    allEntries.sort((a, b) => {
      const tA = a.created_at ? new Date(a.created_at).getTime() : 0;
      const tB = b.created_at ? new Date(b.created_at).getTime() : 0;
      return tB - tA;
    });
    return allEntries;
  }

  function renderDesktopReactorsHtml(reactors) {
    if (!reactors || reactors.length === 0) {
      return `<div style="text-align: center; padding: 22px 10px; color: var(--text-secondary); font-size: 13px;">No reactions yet</div>`;
    }
    return reactors.map((u) => {
      const userObj = { id: u.id, name: u.name, avatar_url: u.avatar };
      const timeFormatted = formatReactionTimestamp(u.created_at);
      return `
      <div class="tg-flyout-reactor-row" onclick="closeTelegramMessageMenu(); openUserProfileModal(${u.id});">
        <div class="tg-flyout-avatar-wrap">
          ${renderAvatarCircleHtml(userObj, "user-avatar-sm", false)}
        </div>
        <div class="tg-flyout-user-info">
          <div class="tg-flyout-user-name">${escapeHtml(u.name)}</div>
          <div class="tg-flyout-user-time"><span class="tg-time-heart">♡</span> ${escapeHtml(timeFormatted)}</div>
        </div>
        <div class="tg-flyout-emoji" title="${escapeHtml(u.emoji)}">${u.emoji}</div>
      </div>
    `;
    }).join("");
  }

  function renderMobileReactorsHtml(reactors) {
    if (!reactors || reactors.length === 0) {
      return `<div style="text-align: center; padding: 26px 10px; color: var(--text-secondary); font-size: 13px;">No reactions yet</div>`;
    }
    return reactors.map((u) => {
      const userObj = { id: u.id, name: u.name, avatar_url: u.avatar };
      const timeFormatted = formatReactionTimestamp(u.created_at);
      return `
      <div class="tg-mobile-reactor-row" onclick="closeTelegramMessageMenu(); openUserProfileModal(${u.id});">
        <div class="tg-mobile-avatar-wrap">
          ${renderAvatarCircleHtml(userObj, "user-avatar-sm", false)}
        </div>
        <div class="tg-mobile-user-info">
          <div class="tg-mobile-user-name">${escapeHtml(u.name)}</div>
          <div class="tg-mobile-user-time"><span class="tg-time-heart">♡</span> ${escapeHtml(timeFormatted)}</div>
        </div>
        <div class="tg-mobile-emoji" title="${escapeHtml(u.emoji)}">${u.emoji}</div>
      </div>
    `;
    }).join("");
  }

  function updateTelegramMenuReactionsRow(reactions) {
    const reactionsItem = document.getElementById("tgMenuReactionsInfoItem");
    const reactionsDivider = document.getElementById("tgMenuDividerReactions");
    const reactionsCountTextEl = document.getElementById("tgMenuReactionsCountText");
    const miniAvatarsEl = document.getElementById("tgMenuMiniAvatars");

    let totalReactions = 0;
    const allReactingUsers = [];
    (reactions || []).forEach((r) => {
      totalReactions += (r.count || 0);
      (r.users || []).forEach((u) => {
        const norm = normalizeReactionUser(u);
        allReactingUsers.push({ ...norm, emoji: r.emoji });
      });
    });

    if (reactionsItem) {
      if (totalReactions > 0) {
        reactionsItem.classList.remove("hidden");
        reactionsItem.style.display = "flex";
        if (reactionsDivider) reactionsDivider.style.display = "block";
        if (reactionsCountTextEl) {
          reactionsCountTextEl.textContent = `${totalReactions} ${totalReactions === 1 ? "Reaction" : "Reactions"}`;
        }

        if (miniAvatarsEl) {
          miniAvatarsEl.innerHTML = allReactingUsers.slice(0, 3).map((u) => {
            const initial = getUserInitial(u.name);
            if (u.avatar) {
              return `<img src="${escapeHtml(u.avatar)}" class="tg-menu-mini-avatar" alt="${escapeHtml(u.name)}" onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='inline-flex';" /><span class="tg-menu-mini-avatar fallback" style="display:none;">${escapeHtml(initial)}</span>`;
            }
            return `<span class="tg-menu-mini-avatar fallback">${escapeHtml(initial)}</span>`;
          }).join("");
        }
      } else {
        reactionsItem.classList.add("hidden");
        reactionsItem.style.display = "none";
        if (reactionsDivider) reactionsDivider.style.display = "none";
      }
    }

    // Live update mobile subview or desktop flyout if currently visible
    const subView = document.getElementById("tgMenuSubView");
    if (subView && subView.style.display !== "none") {
      const subList = document.getElementById("tgMenuMobileReactorsList");
      if (subList && state.activeMenuMsgId) {
        subList.innerHTML = renderMobileReactorsHtml(getMessageReactorsList(state.activeMenuMsgId));
      }
    }
    const flyout = document.getElementById("tgDesktopReactionsFlyout");
    if (flyout && flyout.style.display !== "none") {
      const flyList = document.getElementById("tgDesktopReactorsList");
      if (flyList && state.activeMenuMsgId) {
        flyList.innerHTML = renderDesktopReactorsHtml(getMessageReactorsList(state.activeMenuMsgId));
      }
    }
  }

  function handleReactionsRowMouseEnter(e) {
    if (window.innerWidth <= 768) return;
    clearTimeout(flyoutCloseTimer);
    flyoutCloseTimer = null;

    const msgId = state.activeMenuMsgId;
    if (!msgId) return;

    const reactors = getMessageReactorsList(msgId);
    if (reactors.length === 0) return;

    const flyout = document.getElementById("tgDesktopReactionsFlyout");
    const listEl = document.getElementById("tgDesktopReactorsList");
    const rowEl = document.getElementById("tgMenuReactionsInfoItem");
    const menuEl = document.getElementById("telegramMessageMenu");
    if (!flyout || !listEl || !rowEl || !menuEl) return;

    listEl.innerHTML = renderDesktopReactorsHtml(reactors);
    flyout.classList.remove("hidden");
    flyout.style.display = "flex";

    const menuRect = menuEl.getBoundingClientRect();
    const rowRect = rowEl.getBoundingClientRect();
    const flyoutWidth = flyout.offsetWidth || 250;
    const flyoutHeight = flyout.offsetHeight || 220;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let left = menuRect.right + 6;
    if (left + flyoutWidth > viewportWidth - 10) {
      left = menuRect.left - flyoutWidth - 6;
      if (left < 10) left = 10;
    }

    let top = rowRect.top;
    if (top + flyoutHeight > viewportHeight - 10) {
      top = viewportHeight - flyoutHeight - 10;
    }
    if (top < 10) top = 10;

    flyout.style.left = `${Math.round(left)}px`;
    flyout.style.top = `${Math.round(top)}px`;
  }

  function handleReactionsRowMouseLeave(e) {
    if (window.innerWidth <= 768) return;
    flyoutCloseTimer = setTimeout(() => {
      closeReactionsFlyout();
    }, 220);
  }

  function handleFlyoutMouseEnter() {
    clearTimeout(flyoutCloseTimer);
    flyoutCloseTimer = null;
  }

  function handleFlyoutMouseLeave() {
    flyoutCloseTimer = setTimeout(() => {
      closeReactionsFlyout();
    }, 220);
  }

  function closeReactionsFlyout() {
    clearTimeout(flyoutCloseTimer);
    flyoutCloseTimer = null;
    const flyout = document.getElementById("tgDesktopReactionsFlyout");
    if (flyout) {
      flyout.classList.add("hidden");
      flyout.style.display = "none";
    }
  }

  function handleReactionsRowClick(e) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    const msgId = state.activeMenuMsgId;
    if (!msgId) return;

    if (window.innerWidth <= 768) {
      const reactors = getMessageReactorsList(msgId);
      const subList = document.getElementById("tgMenuMobileReactorsList");
      const mainView = document.getElementById("tgMenuMainView");
      const subView = document.getElementById("tgMenuSubView");
      if (subList) {
        subList.innerHTML = renderMobileReactorsHtml(reactors);
      }
      if (mainView && subView) {
        mainView.classList.remove("active");
        mainView.style.display = "none";
        subView.classList.add("active");
        subView.style.display = "flex";
      }
    } else {
      handleReactionsRowMouseEnter(e);
    }
  }

  function showTgMenuMainView() {
    const mainView = document.getElementById("tgMenuMainView");
    const subView = document.getElementById("tgMenuSubView");
    if (mainView && subView) {
      subView.classList.remove("active");
      subView.style.display = "none";
      mainView.classList.add("active");
      mainView.style.display = "block";
    }
  }

  function renderFullReactionsGrid(filterText = "") {
    const gridEl = document.getElementById("tgFullReactionsGrid");
    if (!gridEl) return;
    const q = (filterText || "").trim().toLowerCase();

    const filtered = TELEGRAM_REACTIONS.filter((emoji) => {
      if (!q) return true;
      if (emoji.includes(q)) return true;
      const kw = EMOJI_KEYWORDS[emoji] || "";
      return kw.toLowerCase().includes(q);
    });

    if (filtered.length === 0) {
      gridEl.innerHTML = `<div class="tg-grid-empty">No reactions found</div>`;
      return;
    }

    gridEl.innerHTML = filtered.map((emoji) => `
    <button
      type="button"
      class="tg-grid-emoji-btn"
      onclick="handleGridReactionSelect('${escapeHtml(emoji)}')"
      title="${escapeHtml(emoji)}"
    >${emoji}</button>
  `).join("");
  }

  function filterReactionsGrid(query) {
    renderFullReactionsGrid(query);
  }

  function handleGridReactionSelect(emoji) {
    const msgId = state.gridTargetMsgId || state.activeMenuMsgId;
    closeFullReactionsGrid();
    closeTelegramMessageMenu();
    if (msgId) {
      toggleMessageReaction(msgId, emoji);
    }
  }

  function openFullReactionsGrid(anchorEl, msgId) {
    if (msgId) {
      state.gridTargetMsgId = Number(msgId);
    } else {
      state.gridTargetMsgId = state.activeMenuMsgId;
    }

    const picker = document.getElementById("fullReactionsPickerPanel");
    const searchInput = document.getElementById("tgReactionSearchInput");
    if (!picker) return;

    if (searchInput) {
      searchInput.value = "";
    }
    renderFullReactionsGrid("");

    picker.classList.remove("hidden");
    picker.style.display = "block";

    const pWidth = picker.offsetWidth || 320;
    const pHeight = picker.offsetHeight || 330;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let left = (vw - pWidth) / 2;
    let top = (vh - pHeight) / 2;

    if (anchorEl && typeof anchorEl.getBoundingClientRect === "function") {
      const aRect = anchorEl.getBoundingClientRect();
      if (aRect.bottom + pHeight + 10 <= vh) {
        top = aRect.bottom + 6;
      } else if (aRect.top - pHeight - 6 >= 10) {
        top = aRect.top - pHeight - 6;
      } else {
        top = Math.max(10, vh - pHeight - 10);
      }
      left = aRect.left + (aRect.width / 2) - (pWidth / 2);
    }

    if (left + pWidth > vw - 10) left = vw - pWidth - 10;
    if (left < 10) left = 10;
    if (top + pHeight > vh - 10) top = vh - pHeight - 10;
    if (top < 10) top = 10;

    picker.style.left = `${Math.round(left)}px`;
    picker.style.top = `${Math.round(top)}px`;
    picker.style.right = "auto";
    picker.style.bottom = "auto";

    if (searchInput && window.innerWidth > 768) {
      setTimeout(() => searchInput.focus(), 60);
    }
  }

  function toggleFullReactionsGrid(e) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    const picker = document.getElementById("fullReactionsPickerPanel");
    if (picker && !picker.classList.contains("hidden") && picker.style.display !== "none") {
      closeFullReactionsGrid();
      return;
    }
    const btn = document.getElementById("tgReactionExpandBtn") || (e ? e.currentTarget : null);
    openFullReactionsGrid(btn, state.activeMenuMsgId);
  }

  function openFullReactionsGridForMsg(e, msgId) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    const btn = e ? e.currentTarget : null;
    openFullReactionsGrid(btn, msgId);
  }

  function closeFullReactionsGrid() {
    state.gridTargetMsgId = null;
    const picker = document.getElementById("fullReactionsPickerPanel");
    if (picker) {
      picker.classList.add("hidden");
      picker.style.display = "none";
    }
  }

  function openTelegramMessageMenu(msgId, clientX, clientY) {
    const idNum = Number(msgId);
    const msg = state.chatMessagesMap.get(idNum);
    if (!msg) return;

    state.activeMenuMsgId = idNum;

    const menu = document.getElementById("telegramMessageMenu");
    const backdrop = document.getElementById("telegramMenuBackdrop");
    const reactionsContainer = document.getElementById("tgMenuReactions");
    const highlightContainer = document.getElementById("tgHighlightedBubbleContainer");
    if (!menu || !backdrop || !reactionsContainer) return;

    // Render quick reaction emojis at top of context menu
    reactionsContainer.innerHTML = TELEGRAM_QUICK_REACTIONS.map((emoji) => `
    <button type="button" class="chat-reaction-btn" onclick="handleMenuReactionClick('${emoji}')" title="${emoji}">${emoji}</button>
  `).join("");
    makeHorizontalScrollable(reactionsContainer);

    // Reset views
    showTgMenuMainView();
    closeReactionsFlyout();
    closeFullReactionsGrid();

    // Check permissions: Edit & Delete only for owner (or admin for delete)
    const currentUserId = Number(state.user?.id) || 0;
    const msgUserId = Number(msg.web_user_id || msg.author_id) || 0;
    const isOwn = Boolean(currentUserId && msgUserId === currentUserId);
    const isAdmin = Boolean(state.user?.is_admin);

    const editBtn = document.getElementById("tgMenuEditBtn");
    const delBtn = document.getElementById("tgMenuDeleteBtn");
    if (editBtn) editBtn.style.display = isOwn ? "flex" : "none";
    if (delBtn) delBtn.style.display = (isOwn || isAdmin) ? "flex" : "none";

    // Populate Reactions row at bottom of menu
    updateTelegramMenuReactionsRow(msg.reactions || []);

    // Populate sent time row at bottom
    const timeDivider = document.getElementById("tgMenuDividerTime");
    const timeItem = document.getElementById("tgMenuTimeItem");
    const timeTextEl = document.getElementById("tgMenuMsgTimeText");
    if (timeItem && timeTextEl && msg.created_at) {
      timeTextEl.textContent = formatReactionTimestamp(msg.created_at);
      timeItem.style.display = "flex";
      if (timeDivider) timeDivider.style.display = "block";
    } else if (timeItem) {
      timeItem.style.display = "none";
      if (timeDivider) timeDivider.style.display = "none";
    }

    // Show backdrop and menu early so dimensions can be measured
    backdrop.classList.remove("hidden");
    backdrop.style.display = "block";
    menu.classList.remove("hidden");
    menu.style.display = "block";

    // Find targeted row & bubble
    const row = document.querySelector(`.chat-bubble-row[data-msg-id="${idNum}"]`);
    const bubble = row ? (row.querySelector(".chat-bubble") || row) : null;

    // Clone bubble into highlight container so it stays crisp & illuminated above dimmed backdrop
    if (highlightContainer && bubble) {
      const bRect = bubble.getBoundingClientRect();
      highlightContainer.style.left = `${bRect.left}px`;
      highlightContainer.style.top = `${bRect.top}px`;
      highlightContainer.style.width = `${bRect.width}px`;
      highlightContainer.style.height = `${bRect.height}px`;
      if (row && row.style.getPropertyValue("--user-chat-color")) {
        highlightContainer.style.setProperty("--user-chat-color", row.style.getPropertyValue("--user-chat-color"));
      }
      if (isOwn) {
        highlightContainer.classList.add("own-message");
      } else {
        highlightContainer.classList.remove("own-message");
      }
      highlightContainer.innerHTML = "";
      const clone = bubble.cloneNode(true);
      clone.removeAttribute("id");
      highlightContainer.appendChild(clone);
      highlightContainer.classList.remove("hidden");
      highlightContainer.style.display = "block";
    }

    // Measure menu dimensions
    const menuWidth = menu.offsetWidth || 270;
    const menuHeight = menu.offsetHeight || 240;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    if (bubble) {
      const rect = bubble.getBoundingClientRect();
      const spaceBelow = viewportHeight - rect.bottom;
      const spaceAbove = rect.top;

      // Prefer placing below if space allows; otherwise place above
      const placeBelow = spaceBelow >= (menuHeight + 14) || spaceBelow >= spaceAbove;

      let top = 0;
      if (placeBelow) {
        top = rect.bottom + 6;
        if (top + menuHeight > viewportHeight - 10) {
          top = viewportHeight - menuHeight - 10;
        }
        if (top < 10) top = 10;
      } else {
        top = rect.top - menuHeight - 6;
        if (top < 10) {
          top = 10;
        }
        if (top + menuHeight > viewportHeight - 10) {
          top = viewportHeight - menuHeight - 10;
        }
      }

      // Horizontal alignment:
      // Own message is aligned to the right side of the screen -> align menu with right edge of bubble
      // Other's message is aligned to the left side of the screen -> align menu with left edge of bubble
      let left = isOwn ? (rect.right - menuWidth) : rect.left;

      // Clamp horizontally to stay comfortably within screen bounds
      const minLeft = 10;
      const maxLeft = viewportWidth - menuWidth - 10;
      if (left > maxLeft) left = maxLeft;
      if (left < minLeft) left = minLeft;

      menu.style.left = `${Math.round(left)}px`;
      menu.style.top = `${Math.round(top)}px`;
      menu.style.right = "auto";
      menu.style.bottom = "auto";
      menu.style.transform = "none";
      menu.style.transformOrigin = `${placeBelow ? "top" : "bottom"} ${isOwn ? "right" : "left"}`;
    } else {
      // Fallback if bubble not found
      let x = clientX !== undefined ? clientX : viewportWidth / 2;
      let y = clientY !== undefined ? clientY : viewportHeight / 2;
      if (x + menuWidth > viewportWidth - 14) x = viewportWidth - menuWidth - 14;
      if (x < 14) x = 14;
      if (y + menuHeight > viewportHeight - 14) y = viewportHeight - menuHeight - 14;
      if (y < 14) y = 14;

      menu.style.left = `${Math.round(x)}px`;
      menu.style.top = `${Math.round(y)}px`;
      menu.style.right = "auto";
      menu.style.bottom = "auto";
      menu.style.transform = "none";
      menu.style.transformOrigin = "top center";
    }

    // Fetch freshest reaction data in background
    apiFetch(`/api/spam-chat/messages/${idNum}/reactions`)
      .then((res) => {
        if (res && res.ok && res.reactions) {
          if (msg) msg.reactions = res.reactions;
          if (state.activeMenuMsgId === idNum) {
            updateTelegramMenuReactionsRow(res.reactions);
          }
        }
      })
      .catch(() => { });
  }

  function closeTelegramMessageMenu() {
    state.activeMenuMsgId = null;
    closeReactionsFlyout();
    closeFullReactionsGrid();
    showTgMenuMainView();

    const menu = document.getElementById("telegramMessageMenu");
    const backdrop = document.getElementById("telegramMenuBackdrop");
    const highlightContainer = document.getElementById("tgHighlightedBubbleContainer");
    if (menu) {
      menu.classList.add("hidden");
      menu.style.display = "none";
    }
    if (backdrop) {
      backdrop.classList.add("hidden");
      backdrop.style.display = "none";
    }
    if (highlightContainer) {
      highlightContainer.classList.add("hidden");
      highlightContainer.style.display = "none";
      highlightContainer.innerHTML = "";
    }
  }

  function handleOpenMessageReactionsModal() {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (msgId) {
      openMessageReactionsModal(msgId);
    }
  }

  async function openMessageReactionsModal(msgId, filterEmoji = null) {
    const idNum = Number(msgId);
    state.viewingReactionsMsgId = idNum;

    const modal = document.getElementById("messageReactionsModal");
    if (!modal) return;

    const msg = state.chatMessagesMap.get(idNum);
    const reactions = msg?.reactions || [];

    modal.classList.remove("hidden");
    modal.style.display = "flex";

    renderMessageReactionsModalContent(idNum, reactions, filterEmoji);

    // Fetch freshest data from server
    try {
      const res = await apiFetch(`/api/spam-chat/messages/${idNum}/reactions`);
      if (res && res.ok && res.reactions) {
        if (msg) msg.reactions = res.reactions;
        if (state.viewingReactionsMsgId === idNum) {
          renderMessageReactionsModalContent(idNum, res.reactions, state.viewingReactionsFilterEmoji);
        }
      }
    } catch (err) {
      // Continue with cached reactions
    }
  }

  function renderMessageReactionsModalContent(msgId, reactions, filterEmoji = null) {
    state.viewingReactionsFilterEmoji = filterEmoji;
    const listContainer = document.getElementById("tgReactionsList");
    const tabsContainer = document.getElementById("tgReactionsTabs");
    const countTitle = document.getElementById("tgReactionsModalTotalCount");
    if (!listContainer) return;

    let allEntries = [];
    let totalCount = 0;
    (reactions || []).forEach((r) => {
      totalCount += (r.count || 0);
      (r.users || []).forEach((u) => {
        const norm = normalizeReactionUser(u);
        allEntries.push({
          ...norm,
          emoji: r.emoji,
        });
      });
    });

    // Sort newest reactions first
    allEntries.sort((a, b) => {
      const tA = a.created_at ? new Date(a.created_at).getTime() : 0;
      const tB = b.created_at ? new Date(b.created_at).getTime() : 0;
      return tB - tA;
    });

    if (countTitle) {
      countTitle.textContent = totalCount;
    }

    // Render filter tabs if multiple reaction emojis exist
    if (tabsContainer) {
      const activeReactions = (reactions || []).filter((r) => r.count > 0);
      if (activeReactions.length > 1) {
        tabsContainer.style.display = "flex";
        tabsContainer.innerHTML = `
        <button
          type="button"
          class="tg-reactions-tab ${filterEmoji === null ? "active" : ""}"
          onclick="filterMessageReactionsModal(null)"
        >
          All <span>${totalCount}</span>
        </button>
        ${activeReactions.map((r) => `
          <button
            type="button"
            class="tg-reactions-tab ${filterEmoji === r.emoji ? "active" : ""}"
            onclick="filterMessageReactionsModal('${escapeHtml(r.emoji)}')"
          >
            <span>${r.emoji}</span> <span>${r.count}</span>
          </button>
        `).join("")}
      `;
        makeHorizontalScrollable(tabsContainer);
      } else {
        tabsContainer.style.display = "none";
      }
    }

    const visibleEntries = filterEmoji
      ? allEntries.filter((e) => e.emoji === filterEmoji)
      : allEntries;

    if (visibleEntries.length === 0) {
      listContainer.innerHTML = `
      <div style="text-align: center; padding: 36px 16px; color: var(--text-secondary);">
        <div style="font-size: 28px; margin-bottom: 6px;">💬</div>
        <p style="font-size: 13.5px;">No reactions found.</p>
      </div>
    `;
      return;
    }

    listContainer.innerHTML = visibleEntries.map((e) => {
      const userObj = {
        id: e.id,
        name: e.name,
        avatar_url: e.avatar,
      };
      const timeFormatted = e.created_at ? formatRelativeTime(e.created_at) : "";
      return `
      <div class="tg-reaction-reactor-row" onclick="closeMessageReactionsModal(); openUserProfileModal(${e.id});">
        <div class="tg-reaction-reactor-avatar-wrap">
          ${renderAvatarCircleHtml(userObj, "user-avatar-md", false)}
        </div>
        <div class="tg-reaction-reactor-info">
          <div class="tg-reaction-reactor-name">${escapeHtml(e.name)}</div>
          <div class="tg-reaction-reactor-time">${timeFormatted ? escapeHtml(timeFormatted) : "Recently"}</div>
        </div>
        <div class="tg-reaction-reactor-emoji" title="${escapeHtml(e.emoji)}">
          ${e.emoji}
        </div>
      </div>
    `;
    }).join("");
  }

  function filterMessageReactionsModal(emoji) {
    const msgId = state.viewingReactionsMsgId;
    if (!msgId) return;
    const msg = state.chatMessagesMap.get(msgId);
    renderMessageReactionsModalContent(msgId, msg?.reactions || [], emoji);
  }

  function closeMessageReactionsModal() {
    state.viewingReactionsMsgId = null;
    state.viewingReactionsFilterEmoji = null;
    const modal = document.getElementById("messageReactionsModal");
    if (modal) {
      modal.classList.add("hidden");
      modal.style.display = "none";
    }
  }

  function handleMenuReactionClick(emoji) {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (msgId) {
      toggleMessageReaction(msgId, emoji);
    }
  }

  function handleMenuReply() {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (msgId) {
      setChatReply(msgId);
    }
  }

  function handleMenuCopy() {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (!msgId) return;

    const msg = state.chatMessagesMap.get(msgId);
    if (!msg) return;

    let text = "";
    if (msg.content_html) {
      const tmp = document.createElement("div");
      tmp.innerHTML = msg.content_html;
      text = tmp.textContent.trim();
    }
    if (!text && msg.image_url) {
      text = msg.image_url;
    }
    if (text) {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => {
          showToast("Text copied to clipboard!");
        }).catch(() => {
          showToast("Failed to copy text.", true);
        });
      } else {
        showToast("Clipboard unavailable in browser.", true);
      }
    }
  }

  function handleMenuEdit() {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (msgId) {
      setChatEdit(msgId);
    }
  }

  function handleMenuDelete() {
    const msgId = state.activeMenuMsgId;
    closeTelegramMessageMenu();
    if (msgId) {
      openDeleteMessageModal(msgId);
    }
  }

  function setChatEdit(msgId) {
    if (!state.user) {
      showToast("Please sign in to edit messages.", true);
      openAuthModal("login");
      return;
    }
    const idNum = Number(msgId);
    const msg = state.chatMessagesMap.get(idNum);
    if (!msg) return;

    // Cancel reply mode if active
    cancelChatReply();

    state.chatEditingMsgId = idNum;

    const strip = document.getElementById("chatEditPreviewStrip");
    const snippetEl = document.getElementById("chatEditSnippet");
    const input = document.getElementById("chatRichInput");

    let snippet = "";
    if (msg.content_html) {
      const tmp = document.createElement("div");
      tmp.innerHTML = msg.content_html;
      snippet = tmp.textContent.trim().replace(/\s+/g, " ");
    }
    if (snippet.length > 65) snippet = snippet.slice(0, 65) + "…";

    if (strip && snippetEl) {
      snippetEl.textContent = snippet || "Message";
      strip.classList.remove("hidden");
      strip.style.removeProperty("display");
    }

    if (input) {
      input.innerHTML = msg.content_html;
      input.focus();
      try {
        const range = document.createRange();
        range.selectNodeContents(input);
        range.collapse(false);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        saveChatInputSelection();
        updateFormatToolbarActiveStates();
      } catch { }
    }
  }

  function cancelChatEdit(e) {
    if (e) {
      if (typeof e.preventDefault === "function") e.preventDefault();
      if (typeof e.stopPropagation === "function") e.stopPropagation();
    }
    state.chatEditingMsgId = null;
    const strip = document.getElementById("chatEditPreviewStrip");
    if (strip) {
      strip.classList.add("hidden");
      strip.style.setProperty("display", "none", "important");
    }
    const input = document.getElementById("chatRichInput");
    if (input) {
      input.innerHTML = "";
      savedChatSelectionRange = null;
      updateFormatToolbarActiveStates();
    }
  }

  function openDeleteMessageModal(msgId) {
    state.pendingDeleteMsgId = Number(msgId);
    const modal = document.getElementById("deleteMessageModal");
    if (modal) {
      modal.classList.remove("hidden");
      modal.style.display = "flex";
    }
  }

  function closeDeleteMessageModal() {
    state.pendingDeleteMsgId = null;
    const modal = document.getElementById("deleteMessageModal");
    if (modal) {
      modal.classList.add("hidden");
      modal.style.display = "none";
    }
  }

  async function confirmDeleteMessage() {
    const msgId = state.pendingDeleteMsgId;
    closeDeleteMessageModal();
    if (!msgId) return;

    try {
      const res = await apiFetch(`/api/spam-chat/messages/${msgId}`, {
        method: "DELETE",
      });
      if (res && res.ok) {
        showToast("Message deleted.");
        state.chatMessagesMap.delete(msgId);
        const row = document.querySelector(`.chat-bubble-row[data-msg-id="${msgId}"]`);
        if (row) {
          row.style.transition = "all 0.25s ease";
          row.style.opacity = "0";
          row.style.transform = "scale(0.85)";
          setTimeout(() => row.remove(), 250);
        }
      }
    } catch (err) {
      showToast(err.message || "Failed to delete message.", true);
    }
  }

  // Mobile touch gestures:
  // 1. Swipe left -> Drag to reply mode
  // 2. Double-tap -> Toggle Heart ❤️ reaction + fly particle
  // 3. Single tap -> Open Telegram context menu (reactions & actions)
  function initChatTouchGestures(rowEl, msgId) {
    if (!rowEl || rowEl.dataset.gesturesInit) return;
    rowEl.dataset.gesturesInit = "1";

    const contentEl = rowEl.querySelector(".chat-bubble-content") || rowEl;

    let touchStartX = 0;
    let touchStartY = 0;
    let touchMoveX = 0;
    let touchIsHorizontal = null;
    let touchStartTarget = null;

    rowEl.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      const touch = e.touches[0];
      touchStartX = touch.clientX;
      touchStartY = touch.clientY;
      touchMoveX = 0;
      touchIsHorizontal = null;
      touchStartTarget = e.target;
    }, { passive: true });

    rowEl.addEventListener("touchmove", (e) => {
      if (e.touches.length !== 1) return;
      const touch = e.touches[0];
      const dx = touch.clientX - touchStartX;
      const dy = touch.clientY - touchStartY;

      if (touchIsHorizontal === null) {
        if (Math.abs(dx) > 8 || Math.abs(dy) > 8) {
          touchIsHorizontal = Math.abs(dx) > Math.abs(dy);
        }
      }

      if (touchIsHorizontal && dx < 0) {
        touchMoveX = dx;
        const pull = Math.max(-80, dx * 0.65);
        contentEl.style.transition = "none";
        contentEl.style.transform = `translateX(${pull}px)`;
        rowEl.classList.add("swiping");

        const indicator = rowEl.querySelector(".chat-swipe-indicator");
        if (indicator) {
          const progress = Math.min(1, Math.abs(dx) / 55);
          indicator.style.transform = `translateY(-50%) scale(${0.6 + progress * 0.5}) rotate(${progress * -45}deg)`;
          if (progress >= 1 && !indicator.dataset.vibrated) {
            indicator.dataset.vibrated = "1";
            if (navigator.vibrate) navigator.vibrate(15);
          }
        }
      }
    }, { passive: true });

    rowEl.addEventListener("touchend", (e) => {
      contentEl.style.transition = "transform 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275)";
      contentEl.style.transform = "translateX(0)";
      rowEl.classList.remove("swiping");
      const indicator = rowEl.querySelector(".chat-swipe-indicator");
      if (indicator) {
        delete indicator.dataset.vibrated;
        indicator.style.transform = "";
      }

      const touch = e.changedTouches[0];
      const dx = touch ? touch.clientX - touchStartX : 0;
      const dy = touch ? touch.clientY - touchStartY : 0;

      // Swipe left past threshold -> Reply mode
      if (touchIsHorizontal && touchMoveX < -50) {
        if (navigator.vibrate) navigator.vibrate(25);
        setChatReply(msgId);
        touchMoveX = 0;
        touchIsHorizontal = null;
        return;
      }

      touchMoveX = 0;
      touchIsHorizontal = null;

      // Tap detection on mobile
      if (Math.abs(dx) < 12 && Math.abs(dy) < 12) {
        if (touchStartTarget && touchStartTarget.closest("a, button, .chat-quote-block, .chat-reaction-btn, .chat-reaction-badge, input, textarea")) {
          return;
        }
        const now = Date.now();
        const last = rowEl._lastTapTime || 0;

        if (now - last < 260) {
          // Double-tap on mobile -> Heart ❤️ reaction (no reply)
          rowEl._lastTapTime = 0;
          rowEl._lastDoubleTapReaction = now;
          clearTimeout(rowEl._singleTapTimer);
          rowEl._singleTapTimer = null;
          if (navigator.vibrate) navigator.vibrate([30, 20]);
          if (touch) {
            spawnHeartParticle(touch.clientX, touch.clientY);
          }
          toggleMessageReaction(msgId, "❤️");
        } else {
          // Single tap -> wait 220ms to check if double tap follows; if not, open Telegram menu docked to message!
          rowEl._lastTapTime = now;
          clearTimeout(rowEl._singleTapTimer);
          const tapX = touch ? touch.clientX : window.innerWidth / 2;
          const tapY = touch ? touch.clientY : window.innerHeight / 2;
          rowEl._singleTapTimer = setTimeout(() => {
            rowEl._singleTapTimer = null;
            if (navigator.vibrate) navigator.vibrate(20);
            openTelegramMessageMenu(msgId, tapX, tapY);
          }, 220);
        }
      }
    }, { passive: true });
  }

  // Desktop hover stability: keeps the Telegram reaction picker open with a grace
  // delay so moving between message bubble and emoji picker feels smooth without abrupt closing.
  function initDesktopHoverPicker(rowEl) {
    if (!rowEl || rowEl.dataset.hoverPickerInit) return;
    rowEl.dataset.hoverPickerInit = "1";

    const pickerEl = rowEl.querySelector(".chat-reaction-picker");
    if (!pickerEl) return;
    makeHorizontalScrollable(pickerEl);

    let closeTimer = null;

    const showPicker = () => {
      if (window.matchMedia("(pointer: coarse)").matches) return;
      clearTimeout(closeTimer);
      document.querySelectorAll(".chat-reaction-picker.desktop-active").forEach((p) => {
        if (p !== pickerEl) p.classList.remove("desktop-active");
      });
      pickerEl.classList.add("desktop-active");
    };

    const scheduleHide = () => {
      if (window.matchMedia("(pointer: coarse)").matches) return;
      clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        pickerEl.classList.remove("desktop-active");
      }, 320);
    };

    rowEl.addEventListener("mouseenter", showPicker);
    rowEl.addEventListener("mouseleave", scheduleHide);
    pickerEl.addEventListener("mouseenter", showPicker);
    pickerEl.addEventListener("mouseleave", scheduleHide);
  }

  function buildSpamMessageHtml(m) {
    state.chatMessagesMap.set(Number(m.id), m);

    const msgUserId = Number(m.web_user_id || m.author_id) || 0;
    const currentUserId = Number(state.user?.id) || 0;
    const isOwn = Boolean(currentUserId && msgUserId === currentUserId);
    const authorId = Number(m.author_id || m.web_user_id) || 0;
    const authorName = m.author_name || (isOwn && state.user?.name ? state.user.name : "AUA Student");
    const authorObj = {
      id: authorId,
      name: authorName,
      avatar_url: m.author_avatar || "",
      bio: m.author_bio || "",
      inbox_link: m.author_inbox_link || "",
    };
    const avatarHtml = renderAvatarCircleHtml(authorObj, "user-avatar-sm", true);
    const color = getChatUserColor(authorId, authorName);

    // Telegram Quote Block
    let replyHtml = "";
    if (m.reply_to) {
      const parentAuthorId = Number(m.reply_to.author_id) || 0;
      const parentAuthorName = m.reply_to.author_name || "AUA Student";
      const parentColor = getChatUserColor(parentAuthorId, parentAuthorName).primary;
      replyHtml = `
      <div class="chat-quote-block" onclick="scrollToMessage(${Number(m.reply_to.id)})" style="--quote-bar-color: ${parentColor};">
        <div class="chat-quote-content">
          <span class="chat-quote-author" style="color: ${parentColor};">${escapeHtml(parentAuthorName)}</span>
          <span class="chat-quote-text">${escapeHtml(m.reply_to.snippet || "Message")}</span>
        </div>
      </div>
    `;
    }

    // Media
    let mediaHtml = "";
    if (m.image_url) {
      const isVid = m.media_type === "video" || /\.(mp4|webm|mov)$/i.test(m.image_url);
      if (isVid) {
        mediaHtml = `
        <div class="chat-media-player-wrapper">
          <video controls playsinline preload="metadata" class="chat-attached-video">
            <source src="${escapeHtml(m.image_url)}">
            Your browser does not support HTML5 video.
          </video>
        </div>
      `;
      } else {
        mediaHtml = `
        <div class="chat-media-player-wrapper">
          <a href="${escapeHtml(m.image_url)}" target="_blank" rel="noopener noreferrer">
            <img src="${escapeHtml(m.image_url)}" class="chat-attached-img" alt="Attachment" loading="lazy" />
          </a>
        </div>
      `;
      }
    }

    // Telegram Floating Quick Reaction Bar with Expand Button
    const pickerButtons = TELEGRAM_QUICK_REACTIONS.map((emoji) => `
    <button
      type="button"
      class="chat-reaction-btn"
      onclick="toggleMessageReaction(${m.id}, '${emoji}')"
      title="${emoji}"
    >${emoji}</button>
  `).join("");

    const expandBtnHtml = `
    <button
      type="button"
      class="tg-reaction-expand-btn"
      onclick="openFullReactionsGridForMsg(event, ${m.id})"
      title="All reactions"
      aria-label="View all reactions"
    >
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </button>
  `;

    const youBadge = isOwn ? `<span class="chat-own-pill">You</span>` : "";

    return `
    <div
      class="chat-bubble-row ${isOwn ? "own-message" : ""}"
      data-msg-id="${m.id}"
      style="--user-chat-color: ${color.primary};"
      ondblclick="handleMessageDblClick(event, ${m.id})"
      oncontextmenu="handleMessageContextMenu(event, ${m.id})"
    >
      <div class="chat-avatar-wrapper" style="--user-avatar-border: ${color.primary};">
        ${avatarHtml}
      </div>

      <div class="chat-bubble-content">
        <!-- Floating Telegram Reaction Picker -->
        <div class="chat-reaction-picker" id="reactionPicker-${m.id}">
          ${pickerButtons}
          ${expandBtnHtml}
        </div>

        <div
          class="chat-bubble"
          style="
            background: linear-gradient(135deg, ${color.subtle}, rgba(11, 18, 36, 0.94));
            border-color: ${color.border};
            box-shadow: 0 4px 18px rgba(0, 0, 0, 0.4), 0 0 16px ${color.glow};
          "
        >
          <div class="chat-bubble-header">
            <div class="chat-author-meta">
              <span class="chat-author-name-text" style="color:${color.primary};">
                ${renderClickableUserNameHtml(authorObj, "chat-author-name")}
              </span>
              ${youBadge}
            </div>
            <div style="display:inline-flex; align-items:center;">
              <span class="chat-timestamp">${formatDateTime(m.created_at)}</span>
              ${m.is_edited ? `<span class="chat-edited-tag">edited</span>` : ""}
            </div>
          </div>

          ${replyHtml}
          ${m.content_html ? `<div class="chat-bubble-text">${m.content_html}</div>` : ""}
          ${mediaHtml}
        </div>

        <!-- Telegram Reaction Chips -->
        <div class="chat-reactions-wrapper" id="chatReactions-${m.id}">
          ${renderReactionsHtml(m.id, m.reactions || [])}
        </div>
      </div>

      <!-- Mobile Swipe Reply Indicator -->
      <div class="chat-swipe-indicator">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="9 17 4 12 9 7"></polyline>
          <path d="M20 18v-2a4 4 0 0 0-4-4H4"></path>
        </svg>
      </div>
    </div>
  `;
  }

  let chatScrollRaf = null;

  function handleChatScroll(stream) {
    if (!stream || chatScrollRaf) return;
    chatScrollRaf = requestAnimationFrame(() => {
      chatScrollRaf = null;
      if (state.activeMenuMsgId) {
        closeTelegramMessageMenu();
      }
      const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
      state.chatIsScrolledUp = distanceFromBottom > 130;
      const scrollBtn = document.getElementById("chatScrollBottomBtn");
      if (!state.chatIsScrolledUp && scrollBtn) {
        scrollBtn.classList.add("hidden");
        state.chatUnreadSinceScroll = 0;
        const unreadPill = document.getElementById("chatScrollUnreadCount");
        if (unreadPill) unreadPill.classList.add("hidden");
      }
    });
  }

  function scrollChatToBottom(smooth = true) {
    const stream = document.getElementById("spamMessagesStream");
    if (!stream) return;
    stream.scrollTo({
      top: stream.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
    state.chatIsScrolledUp = false;
    state.chatUnreadSinceScroll = 0;
    const scrollBtn = document.getElementById("chatScrollBottomBtn");
    if (scrollBtn) scrollBtn.classList.add("hidden");
    const unreadPill = document.getElementById("chatScrollUnreadCount");
    if (unreadPill) unreadPill.classList.add("hidden");
  }

  function getCleanRichChatContent(input) {
    if (!input) return "";
    let rawHtml = (input.innerHTML || "").replace(/\u200B/g, "");
    const tmp = document.createElement("div");
    tmp.innerHTML = rawHtml;

    // Prune empty formatting tags
    let cleaned = true;
    while (cleaned) {
      cleaned = false;
      tmp.querySelectorAll("b, strong, i, em, u, s, strike, del, code").forEach((el) => {
        if (!el.textContent.trim() && !el.querySelector("br, img")) {
          el.remove();
          cleaned = true;
        }
      });
    }

    const plainText = (tmp.textContent || "").trim();
    return plainText ? tmp.innerHTML.trim() : "";
  }

  async function sendSpamChatMessage(e) {
    if (e) e.preventDefault();
    if (!state.user) {
      showToast("Please sign in to post in Chat.", true);
      openAuthModal("login");
      return;
    }

    const input = document.getElementById("chatRichInput");
    const content = input ? getCleanRichChatContent(input) : "";

    const media_url = state.chatPendingMediaUrl || state.chatPendingImageUrl;
    const media_type = state.chatPendingMediaType;

    if (!content && !media_url) {
      showToast("Please enter a message or attach an image, GIF, or video.", true);
      return;
    }

    const sendBtn = document.getElementById("chatSendBtn");
    if (sendBtn) sendBtn.disabled = true;

    // Handle Edit mode
    if (state.chatEditingMsgId) {
      const editId = state.chatEditingMsgId;
      if (!content) {
        showToast("Message content cannot be empty.", true);
        if (sendBtn) sendBtn.disabled = false;
        return;
      }

      try {
        const res = await apiFetch(`/api/spam-chat/messages/${editId}`, {
          method: "PUT",
          body: JSON.stringify({ content: content }),
        });
        if (res && res.message) {
          cancelChatEdit();
          showToast("Message edited.");
        }
      } catch (err) {
        showToast(err.message || "Failed to edit message.", true);
      } finally {
        if (sendBtn) sendBtn.disabled = false;
        if (input && window.innerWidth > 768) input.focus();
      }
      return;
    }

    try {
      closeEmojiPicker();
      if (input) {
        input.innerHTML = "";
        savedChatSelectionRange = null;
        updateFormatToolbarActiveStates();
      }
      clearChatMediaAttachment();

      const payload = {
        content: content,
        image_url: media_url || null,
        media_type: media_type || null,
        reply_to_id: state.chatReplyTo ? state.chatReplyTo.id : null,
      };

      const res = await apiFetch("/api/spam-chat/messages", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      if (res && res.message) {
        cancelChatReply();
        scrollChatToBottom(true);
      }
    } catch (err) {
      showToast(err.message || "Failed to send message.", true);
    } finally {
      if (sendBtn) sendBtn.disabled = false;
      if (input && window.innerWidth > 768) input.focus();
    }
  }

  function initSpamChatWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws/spam-chat`;
    try {
      const ws = new WebSocket(wsUrl);
      state.ws = ws;

      ws.onopen = () => {
        try {
          if (state.token) {
            ws.send(JSON.stringify({ type: "auth", token: state.token }));
          }
        } catch { }
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "new_message" && data.message) {
            const msg = data.message;
            const stream = document.getElementById("spamMessagesStream");
            if (stream && !stream.querySelector(`[data-msg-id="${msg.id}"]`)) {
              if (stream.querySelector("h3")) {
                stream.innerHTML = "";
              }
              stream.insertAdjacentHTML("beforeend", buildSpamMessageHtml(msg));
              const newRow = stream.querySelector(`[data-msg-id="${msg.id}"]`);
              if (newRow) {
                initChatTouchGestures(newRow, msg.id);
                initDesktopHoverPicker(newRow);
              }

              const isOwn = state.user && msg.web_user_id === state.user.id;
              if (state.chatIsScrolledUp && !isOwn) {
                state.chatUnreadSinceScroll++;
                const scrollBtn = document.getElementById("chatScrollBottomBtn");
                const unreadPill = document.getElementById("chatScrollUnreadCount");
                if (scrollBtn) {
                  scrollBtn.classList.remove("hidden");
                  if (unreadPill) {
                    unreadPill.textContent = state.chatUnreadSinceScroll;
                    unreadPill.classList.remove("hidden");
                  }
                }
              } else {
                scrollChatToBottom(true);
              }
            }

            if (state.activeView === "spam-chat") {
              if (state.user) {
                apiFetch("/api/spam-chat/read", {
                  method: "POST",
                  body: JSON.stringify({ latest_message_id: msg.id }),
                }).catch(() => { });
              }
            } else if (!state.user || msg.web_user_id !== state.user.id) {
              updateUnreadBadge(state.unreadSpamCount + 1);
            }
          } else if (data.type === "message_reaction" && data.message_id) {
            updateMessageReactionsDOM(data.message_id, data.reactions || []);
          } else if (data.type === "message_edit" && data.message) {
            const updated = data.message;
            state.chatMessagesMap.set(Number(updated.id), updated);
            const row = document.querySelector(`.chat-bubble-row[data-msg-id="${updated.id}"]`);
            if (row) {
              const textContainer = row.querySelector(".chat-bubble-text");
              if (textContainer) {
                textContainer.innerHTML = updated.content_html;
              }
              const header = row.querySelector(".chat-bubble-header");
              if (header && !header.querySelector(".chat-edited-tag")) {
                const timeEl = header.querySelector(".chat-timestamp");
                if (timeEl) {
                  timeEl.insertAdjacentHTML("afterend", `<span class="chat-edited-tag">edited</span>`);
                }
              }
            }
          } else if (data.type === "message_delete" && data.message_id) {
            const delId = Number(data.message_id);
            state.chatMessagesMap.delete(delId);
            const row = document.querySelector(`.chat-bubble-row[data-msg-id="${delId}"]`);
            if (row) {
              row.style.transition = "all 0.25s ease";
              row.style.opacity = "0";
              row.style.transform = "scale(0.85)";
              setTimeout(() => row.remove(), 250);
            }
          } else if (data.type === "new_notification") {
            if (state.user && Number(data.recipient_id) === Number(state.user.id)) {
              const notif = data.notification;
              if (notif) {
                state.notifications = [notif, ...state.notifications.filter((n) => Number(n.id) !== Number(notif.id))];
                state.unreadNotificationCount = (data.unread_count !== undefined) ? Number(data.unread_count) : (state.unreadNotificationCount + 1);
                updateNotificationBadge();
                const popover = document.getElementById("notificationsPopover");
                if (popover && !popover.classList.contains("hidden") && popover.style.display !== "none") {
                  renderNotificationsList();
                }
                showToast(`🔔 ${notif.title}`);
              }
            }
          }
        } catch { }
      };

      ws.onclose = () => {
        setTimeout(initSpamChatWebSocket, 4000);
      };
    } catch { }
  }

  // Dismiss open Telegram reaction pickers, full grid, flyout, context menu, notifications popover, or profile menu when clicking outside
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#notificationsPopover") && !e.target.closest("#navbarNotifBtn")) {
      closeNotificationPopover();
    }
    if (!e.target.closest("#profileMenuPopover") && !e.target.closest("#navbarProfileBtn")) {
      closeProfileMenu();
    }
    if (!e.target.closest(".chat-reaction-picker") && !e.target.closest(".tg-reaction-expand-btn") && !e.target.closest("#fullReactionsPickerPanel")) {
      document.querySelectorAll(".chat-reaction-picker.visible, .chat-reaction-picker.desktop-active").forEach((p) => {
        p.classList.remove("visible");
        p.classList.remove("desktop-active");
      });
    }
    if (!e.target.closest("#telegramMessageMenu") && !e.target.closest(".chat-bubble-row") && !e.target.closest("#fullReactionsPickerPanel") && !e.target.closest("#tgDesktopReactionsFlyout")) {
      closeTelegramMessageMenu();
    }
    if (!e.target.closest("#fullReactionsPickerPanel") && !e.target.closest(".tg-reaction-expand-btn")) {
      closeFullReactionsGrid();
    }
  });

  // Cancel Telegram menu, full grid, reactions modal, notifications popover, profile menu, edit mode, or reply banner with Escape key
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const delAccModal = document.getElementById("deleteAccountModal");
      if (delAccModal && !delAccModal.classList.contains("hidden") && delAccModal.style.display !== "none") {
        closeDeleteAccountModal();
        return;
      }
      const profMenu = document.getElementById("profileMenuPopover");
      if (profMenu && !profMenu.classList.contains("hidden") && profMenu.style.display !== "none") {
        closeProfileMenu();
        return;
      }
      const notifPop = document.getElementById("notificationsPopover");
      if (notifPop && !notifPop.classList.contains("hidden") && notifPop.style.display !== "none") {
        closeNotificationPopover();
      } else {
        const fullPicker = document.getElementById("fullReactionsPickerPanel");
        if (fullPicker && !fullPicker.classList.contains("hidden") && fullPicker.style.display !== "none") {
          closeFullReactionsGrid();
        } else if (state.viewingReactionsMsgId) {
          closeMessageReactionsModal();
        } else if (state.activeMenuMsgId) {
          closeTelegramMessageMenu();
        } else if (state.pendingDeleteMsgId) {
          closeDeleteMessageModal();
        } else if (state.chatEditingMsgId) {
          cancelChatEdit();
        } else if (state.chatReplyTo) {
          cancelChatReply();
        }
      }
    }
  });

  /* --------------------------------------------------------------------------
     7. <Profile> User Profile & /link Helper
     -------------------------------------------------------------------------- */

  function renderProfileView() {
    const container = document.getElementById("profileViewContent");
    if (!container) return;

    if (!state.user) {
      container.innerHTML = `
      <div class="glass-card" style="text-align:center; padding:48px 20px;">
        <div style="font-size:38px; margin-bottom:10px;">👤</div>
        <h3 style="font-size:20px; font-weight:800;">Sign In to Manage Your Profile</h3>
        <p style="color:var(--text-secondary); font-size:14px; margin:8px auto 20px; max-width:460px;">
          Sign in with your <code>@edu.aua.am</code> or <code>@alumni.aua.am</code> email to customize your picture, bio, and personal <code>/link</code> inbox.
        </p>
        <div style="display:flex; justify-content:center; gap:12px;">
          <button class="btn-glass" onclick="openAuthModal('login')">Sign In</button>
          <button class="btn-illuminated" onclick="openAuthModal('register')">Register</button>
        </div>
      </div>
    `;
      return;
    }

    const u = state.user;
    registerKnownUser(u);
    const avatarDisplay = renderAvatarCircleHtml(u, "user-avatar-xl", true);

    container.innerHTML = `
    <div class="glass-panel" style="max-width: 680px; padding: 28px;">
      <form onsubmit="submitProfileUpdate(event)">
        <div style="display:flex; align-items:center; gap:20px; margin-bottom:24px; flex-wrap:wrap;">
          ${avatarDisplay}
          <div style="flex:1; min-width:200px;">
            <div style="font-size:20px; font-weight:800; display:inline-block; cursor:pointer;" onmouseenter="showAvatarHoverPreview(event, ${Number(u.id)})" onmousemove="moveAvatarHoverPreview(event)" onmouseleave="hideAvatarHoverPreview()">${escapeHtml(u.name)}</div>
            <div style="font-size:13px; color:var(--neon-blue-bright); margin-bottom:12px;">${escapeHtml(u.email)}</div>
            <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
              <label class="btn-glass" style="cursor:pointer; font-size:12.5px; padding:7px 14px;">
                📷 Change Profile Picture
                <input type="file" accept="image/*" style="display:none;" onchange="handleProfileAvatarUpload(this)" />
              </label>
              <button
                type="button"
                class="btn-glass"
                style="font-size:12.5px; padding:7px 14px;"
                onclick="openUserProfileModal(${Number(u.id)})"
              >
                👁️ Preview Public Profile
              </button>
            </div>
            <input type="hidden" id="profileAvatarUrlInput" value="${escapeHtml(u.avatar_url || "")}" />
          </div>
        </div>

        <div style="margin-bottom: 16px;">
          <label class="field-label">👤 Display Name</label>
          <input type="text" id="profileNameInput" class="input-glass" value="${escapeHtml(u.name)}" required />
        </div>

        <div style="margin-bottom: 16px;">
          <label class="field-label">📝 Bio</label>
          <textarea id="profileBioInput" class="textarea-glass" rows="3" placeholder="Tell fellow AUA students a bit about yourself, your major, or interests...">${escapeHtml(u.bio || "")}</textarea>
        </div>

        <div style="margin-bottom: 22px;">
          <label class="field-label">🎭 Anonymous Inbox Link (generated via <code>/link</code> in @aua_anonymous_bot)</label>
          <input
            type="text"
            id="profileInboxLinkInput"
            class="input-glass"
            value="${escapeHtml(u.inbox_link || "")}"
            placeholder="https://t.me/aua_anonymous_bot?start=msg_..."
          />
          <div class="moodle-password-note" style="margin-top:10px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;">
            <div>
              <b>${u.inbox_link ? "Need to grab or copy your /link again?" : "Don't have an inbox link yet?"}</b><br/>
              Click the button to open <b>@aua_anonymous_bot</b> in Telegram and automatically run <code>/link</code> so you can copy &amp; paste it here!
            </div>
            <a
              href="https://t.me/aua_anonymous_bot?start=getlink"
              target="_blank"
              rel="noopener noreferrer"
              class="btn-illuminated"
              style="padding:8px 14px; font-size:12.5px; flex-shrink:0;"
            >
              ⚡ Open Bot &amp; Auto-Run /link
            </a>
          </div>
        </div>

        <div style="display:flex; gap:12px; margin-top:20px; flex-wrap:wrap;">
          <button type="submit" class="btn-illuminated" style="flex:1;">
            💾 Save Profile Changes
          </button>
          <button type="button" class="btn-danger-glass" onclick="logoutUser()" style="padding:10px 20px;">
            🚪 Sign Out
          </button>
        </div>
      </form>
    </div>
  `;
  }

  async function handleProfileAvatarUpload(inputEl) {
    const file = inputEl.files?.[0];
    if (!file) return;
    const currentName = document.getElementById("profileNameInput")?.value.trim() || state.user?.name || "";
    const currentBio = document.getElementById("profileBioInput")?.value.trim() ?? (state.user?.bio || "");
    const currentInbox = document.getElementById("profileInboxLinkInput")?.value.trim() ?? (state.user?.inbox_link || "");

    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await apiFetch("/api/upload", { method: "POST", body: fd });
      const newAvatarUrl = res.url;

      const saved = await apiFetch("/api/profile", {
        method: "PUT",
        body: JSON.stringify({
          name: currentName,
          bio: currentBio,
          avatar_url: newAvatarUrl,
          inbox_link: currentInbox,
        }),
      });

      state.user = saved.user;
      registerKnownUser(state.user);
      renderProfileView();
      renderNavbarAuth();
      showToast("Profile picture updated and saved! ✨");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function submitProfileUpdate(e) {
    e.preventDefault();
    const name = document.getElementById("profileNameInput").value.trim();
    const bio = document.getElementById("profileBioInput").value.trim();
    const avatar_url = document.getElementById("profileAvatarUrlInput").value.trim();
    const inbox_link = document.getElementById("profileInboxLinkInput").value.trim();

    try {
      const res = await apiFetch("/api/profile", {
        method: "PUT",
        body: JSON.stringify({ name, bio, avatar_url, inbox_link }),
      });
      state.user = res.user;
      registerKnownUser(state.user);
      renderNavbarAuth();
      renderProfileView();
      showToast("Profile updated successfully! ✨");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* --------------------------------------------------------------------------
     8. <Feedbacks> Instructors & Courses Reviews
     -------------------------------------------------------------------------- */

  async function initFeedbacksView() {
    if (!state.fbCatalogLoaded) {
      await loadFeedbacksCatalog();
    }
    await loadAcademicFeedbacks();
  }

  async function loadFeedbacksCatalog() {
    try {
      const data = await apiFetch("/api/feedbacks/catalog");
      state.fbInstructors = data.instructors || [];
      state.fbCourses = data.courses || [];
      state.fbCatalogLoaded = true;
    } catch (err) {
      showToast("Could not load instructors & courses catalog: " + err.message, true);
    }
  }

  function getActiveFeedbackCatalog() {
    return state.fbType === "instructor" ? state.fbInstructors : state.fbCourses;
  }

  function switchFeedbackSubtab(type) {
    state.fbType = type === "course" ? "course" : "instructor";
    state.fbSelectedTarget = null;
    state.fbFilterTarget = null;

    document.getElementById("fbTabInstructor").classList.toggle("active", state.fbType === "instructor");
    document.getElementById("fbTabCourse").classList.toggle("active", state.fbType === "course");

    const isInst = state.fbType === "instructor";
    document.getElementById("fbFormTitle").textContent = isInst
      ? "Leave Instructor Feedback"
      : "Leave Course Feedback";
    document.getElementById("fbFormSub").textContent = isInst
      ? "Select an AUA lecturer or professor and share your experience with fellow students."
      : "Select an AUA course and share your review on content, difficulty, and workload.";
    document.getElementById("fbPickerLabel").textContent = isInst
      ? "Select Instructor / Lecturer"
      : "Select Course";
    document.getElementById("fbTargetSearch").placeholder = isInst
      ? "Search instructor name (e.g. Arsen Chilingaryan)..."
      : "Search course code or title (e.g. BAB101, Calculus)...";
    document.getElementById("fbFilterSearch").value = "";
    document.getElementById("fbFilterSearch").placeholder = isInst
      ? "Search target instructor or keywords to view feedbacks..."
      : "Search target course or keywords to view feedbacks...";

    renderSelectedFeedbackFormTarget();
    loadAcademicFeedbacks();
  }

  function setFeedbackRating(val) {
    state.fbRating = Math.max(1, Math.min(5, Number(val) || 5));
    const buttons = document.querySelectorAll("#fbStarPicker .star-btn");
    buttons.forEach((btn, idx) => {
      btn.classList.toggle("active", idx < state.fbRating);
    });
    const textEl = document.getElementById("fbRatingText");
    if (textEl) textEl.textContent = `${state.fbRating} / 5`;
  }

  function openFeedbackTargetDropdown() {
    const val = document.getElementById("fbTargetSearch")?.value || "";
    filterFeedbackTargetDropdown(val);
  }

  function filterFeedbackTargetDropdown(query) {
    const dropdown = document.getElementById("fbTargetDropdown");
    if (!dropdown) return;

    const q = (query || "").trim().toLowerCase();
    const catalog = getActiveFeedbackCatalog();
    const matches = catalog
      .filter((item) => {
        if (!q) return true;
        return `${item.key} ${item.title} ${item.subtitle}`.toLowerCase().includes(q);
      })
      .slice(0, 45);

    if (matches.length === 0) {
      dropdown.innerHTML = `<div style="padding:12px; color:var(--text-secondary); font-size:13px;">No matching ${state.fbType}s found.</div>`;
    } else {
      dropdown.innerHTML = matches
        .map((item, i) => {
          const ratingInfo =
            item.reviews_count > 0
              ? `<span style="color:var(--neon-blue-bright); font-size:12px;">&#9733; ${item.avg_rating} (${item.reviews_count})</span>`
              : `<span style="color:var(--text-secondary); font-size:11.5px;">No reviews yet</span>`;
          const subLabel =
            state.fbType === "instructor"
              ? `Courses: ${escapeHtml(item.subtitle || "AUA")}`
              : `Instructors: ${escapeHtml(item.subtitle || "TBA")}`;
          const encodedKey = encodeJsAttr(item.key);
          return `
          <div class="course-option" onclick="selectFeedbackFormTarget('${encodedKey}')">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
              <div class="course-option-title">${escapeHtml(item.title)}</div>
              ${ratingInfo}
            </div>
            <div class="course-option-meta">${subLabel}</div>
          </div>
        `;
        })
        .join("");
    }
    dropdown.classList.add("open");
  }

  function selectFeedbackFormTarget(encodedKey) {
    const key = decodeURIComponent(encodedKey);
    const catalog = getActiveFeedbackCatalog();
    const found = catalog.find((x) => x.key.toUpperCase() === key.toUpperCase());
    if (!found) return;

    openMobileFormPanel("fbFormPanel", "fbMobileFormToggleBtn", "Hide Feedback Form");
    state.fbSelectedTarget = found;
    const dropdown = document.getElementById("fbTargetDropdown");
    if (dropdown) dropdown.classList.remove("open");
    document.getElementById("fbTargetSearch").value = "";
    renderSelectedFeedbackFormTarget();
    const panel = document.getElementById("fbFormPanel");
    if (panel && window.innerWidth <= 980) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function clearFeedbackFormTarget() {
    state.fbSelectedTarget = null;
    renderSelectedFeedbackFormTarget();
  }

  function renderSelectedFeedbackFormTarget() {
    const container = document.getElementById("fbSelectedTargetContainer");
    if (!container) return;
    const t = state.fbSelectedTarget;
    if (!t) {
      container.innerHTML = "";
      return;
    }
    const subLabel =
      state.fbType === "instructor"
        ? `Courses: ${escapeHtml(t.subtitle || "AUA")}`
        : `Instructors: ${escapeHtml(t.subtitle || "TBA")}`;
    container.innerHTML = `
    <div class="selected-course-chip">
      <div>
        <div style="font-size:13.5px; font-weight:800; color:var(--text-white);">${escapeHtml(t.title)}</div>
        <div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">${subLabel}</div>
      </div>
      <button type="button" class="btn-glass" style="padding:3px 9px; font-size:12px;" onclick="clearFeedbackFormTarget()">&times;</button>
    </div>
  `;
  }

  // Right-side Filter & Target Search Autocomplete
  function openFeedbackFilterSuggestions() {
    const val = document.getElementById("fbFilterSearch")?.value || "";
    renderFeedbackFilterDropdown(val);
  }

  let _fbSearchTimer = null;
  function onFeedbackFilterInput(val) {
    renderFeedbackFilterDropdown(val);
    clearTimeout(_fbSearchTimer);
    _fbSearchTimer = setTimeout(() => {
      state.fbFilterTarget = null;
      loadAcademicFeedbacks();
    }, 220);
  }

  function renderFeedbackFilterDropdown(query) {
    const dropdown = document.getElementById("fbFilterDropdown");
    if (!dropdown) return;
    const q = (query || "").trim().toLowerCase();
    if (!q) {
      dropdown.classList.remove("open");
      return;
    }
    const catalog = getActiveFeedbackCatalog();
    const matches = catalog
      .filter((item) => `${item.key} ${item.title} ${item.subtitle}`.toLowerCase().includes(q))
      .slice(0, 15);

    if (matches.length === 0) {
      dropdown.classList.remove("open");
      return;
    }

    dropdown.innerHTML = matches
      .map((item) => {
        const encodedKey = encodeJsAttr(item.key);
        return `
        <div class="course-option" onclick="selectFeedbackFilterTarget('${encodedKey}')">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
            <div class="course-option-title">${escapeHtml(item.title)}</div>
            <span style="font-size:12px; color:var(--neon-blue-bright);">
              ${item.reviews_count > 0 ? `&#9733; ${item.avg_rating} (${item.reviews_count})` : "View feedbacks"}
            </span>
          </div>
          <div class="course-option-meta">${escapeHtml(item.subtitle || "")}</div>
        </div>
      `;
      })
      .join("");
    dropdown.classList.add("open");
  }

  function selectFeedbackFilterTarget(encodedKey) {
    const key = decodeURIComponent(encodedKey);
    const catalog = getActiveFeedbackCatalog();
    const found = catalog.find((x) => x.key.toUpperCase() === key.toUpperCase()) || {
      key,
      title: key,
      subtitle: "",
    };
    state.fbFilterTarget = found;
    const dropdown = document.getElementById("fbFilterDropdown");
    if (dropdown) dropdown.classList.remove("open");
    document.getElementById("fbFilterSearch").value = found.title;
    loadAcademicFeedbacks();
  }

  function clearFeedbackTargetFilter() {
    state.fbFilterTarget = null;
    const input = document.getElementById("fbFilterSearch");
    if (input) input.value = "";
    loadAcademicFeedbacks();
  }

  async function loadAcademicFeedbacks() {
    const rawQuery = (document.getElementById("fbFilterSearch")?.value || "").trim();
    const params = new URLSearchParams();
    params.set("type", state.fbType);

    if (state.fbFilterTarget) {
      params.set("target_key", state.fbFilterTarget.key);
    } else if (rawQuery) {
      params.set("q", rawQuery);
    }

    const clearBtn = document.getElementById("fbClearFilterBtn");
    if (clearBtn) {
      clearBtn.style.display = state.fbFilterTarget || rawQuery ? "inline-flex" : "none";
    }

    try {
      const data = await apiFetch(`/api/feedbacks?${params.toString()}`);
      state.fbItems = data.feedbacks || [];
      state.fbSummary = data.summary || { total_reviews: 0, avg_rating: 0 };
      renderFeedbacksList();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderStarsHtml(rating) {
    const r = Math.round(Number(rating) || 0);
    let out = "";
    for (let i = 1; i <= 5; i++) {
      out += `<span style="color:${i <= r ? "var(--neon-blue-bright)" : "rgba(148,163,184,0.3)"};">&#9733;</span>`;
    }
    return out;
  }

  function renderFeedbacksList() {
    const overviewEl = document.getElementById("fbTargetOverviewArea");
    const listEl = document.getElementById("feedbacksList");
    if (!listEl || !overviewEl) return;

    // Render target overview banner if a specific target is selected
    if (state.fbFilterTarget) {
      const t = state.fbFilterTarget;
      const avgStr = state.fbSummary.total_reviews > 0 ? `${state.fbSummary.avg_rating} / 5.0` : "No ratings yet";
      const encodedKey = encodeJsAttr(t.key);
      overviewEl.innerHTML = `
      <div class="target-overview-banner">
        <div>
          <div style="font-size:11.5px; font-weight:700; color:var(--neon-blue-bright); text-transform:uppercase; letter-spacing:0.06em;">
            ${state.fbType === "instructor" ? "Instructor Profile" : "Course Overview"}
          </div>
          <h3 style="font-size:20px; font-weight:800; color:var(--text-white); margin-top:2px;">
            ${escapeHtml(t.title)}
          </h3>
          ${t.subtitle
          ? `<div style="font-size:13px; color:var(--text-secondary); margin-top:3px;">
                  ${state.fbType === "instructor" ? "Courses Taught: " : "Instructors: "} ${escapeHtml(t.subtitle)}
                 </div>`
          : ""
        }
        </div>
        <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
          <div class="rating-badge" style="font-size:14px; padding:7px 14px;">
            &#9733; ${escapeHtml(avgStr)} (${state.fbSummary.total_reviews} ${state.fbSummary.total_reviews === 1 ? "review" : "reviews"})
          </div>
          <button class="btn-illuminated" style="padding:8px 14px; font-size:12.5px;" onclick="selectFeedbackFormTarget('${encodedKey}')">
            Review This ${state.fbType === "instructor" ? "Instructor" : "Course"}
          </button>
        </div>
      </div>
    `;
    } else {
      overviewEl.innerHTML = "";
    }

    if (state.fbItems.length === 0) {
      listEl.innerHTML = `
      <div class="glass-card" style="text-align:center; padding:42px 20px;">
        <h3 style="font-size:18px; font-weight:700;">No Feedbacks Found Yet</h3>
        <p style="color:var(--text-secondary); font-size:13.5px; margin-top:6px;">
          Be the first to select ${state.fbType === "instructor" ? "an instructor" : "a course"} on the left and share your feedback!
        </p>
      </div>
    `;
      return;
    }

    listEl.innerHTML = state.fbItems
      .map((fb) => {
        const encodedKey = encodeJsAttr(fb.target_key);
        const ownerDeleteBtn = fb.is_owner
          ? `<button class="btn-danger-glass" onclick="deleteAcademicFeedback(${fb.id})">Remove</button>`
          : "";

        const authorFooterHtml =
          !fb.is_anonymous && fb.author_id
            ? (() => {
              const authorObj = {
                id: fb.author_id,
                name: fb.author_name || "AUA Student",
                avatar_url: fb.author_avatar || "",
                bio: fb.author_bio || "",
                inbox_link: fb.author_inbox_link || "",
              };
              return `
                <div style="display:inline-flex; align-items:center; gap:7px;">
                  ${renderAvatarCircleHtml(authorObj, "user-avatar-xs", true)}
                  <span>By ${renderClickableUserNameHtml(authorObj, "fb-author-name")}</span>
                </div>
              `;
            })()
            : `
            <div style="display:inline-flex; align-items:center; gap:7px;">
              <span class="user-avatar-circle user-avatar-xs"><span class="user-avatar-fallback" style="font-size:10px;">🎭</span></span>
              <span>By <b style="color:var(--text-ice);">${escapeHtml(fb.author_name)}</b></span>
            </div>
          `;

        return `
        <div class="glass-card">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:10px;">
            <button
              type="button"
              class="feedback-target-pill"
              onclick="selectFeedbackFilterTarget('${encodedKey}')"
              title="Click to view all feedbacks for ${escapeHtml(fb.target_title)}"
            >
              <span>${escapeHtml(fb.target_title)}</span>
            </button>

            <div style="display:flex; align-items:center; gap:10px;">
              <span class="rating-badge">
                ${renderStarsHtml(fb.rating)}
                <span style="margin-left:4px;">${fb.rating}/5</span>
              </span>
              ${ownerDeleteBtn}
            </div>
          </div>

          <div style="font-size:14.5px; color:var(--text-white); line-height:1.6; white-space:pre-wrap; margin:10px 0;">${escapeHtml(fb.comment)}</div>

          <div style="display:flex; align-items:center; justify-content:space-between; border-top:1px solid rgba(56,189,248,0.12); padding-top:8px; margin-top:10px; font-size:12px; color:var(--text-secondary);">
            ${authorFooterHtml}
            <span>${formatDateTime(fb.created_at)}</span>
          </div>
        </div>
      `;
      })
      .join("");
  }

  async function submitAcademicFeedback(e) {
    e.preventDefault();
    if (!state.user) {
      showToast("Please sign in with your @edu.aua.am account to leave feedback.", true);
      openAuthModal("login");
      return;
    }

    if (!state.fbSelectedTarget) {
      showToast(
        `Please select ${state.fbType === "instructor" ? "an instructor" : "a course"} from the search box first.`,
        true
      );
      return;
    }

    const comment = document.getElementById("fbCommentInput").value.trim();
    const is_anonymous = document.getElementById("fbAnonymousCheck").checked;

    try {
      await apiFetch("/api/feedbacks", {
        method: "POST",
        body: JSON.stringify({
          target_type: state.fbType,
          target_key: state.fbSelectedTarget.key,
          target_title: state.fbSelectedTarget.title,
          target_subtitle: state.fbSelectedTarget.subtitle || "",
          rating: state.fbRating,
          comment,
          is_anonymous,
        }),
      });

      document.getElementById("fbCommentInput").value = "";
      closeMobileFormPanel("fbFormPanel", "fbMobileFormToggleBtn", "+ Leave a Feedback");
      showToast("Feedback published! Thank you for helping fellow AUA students.");
      await loadFeedbacksCatalog();
      await loadAcademicFeedbacks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteAcademicFeedback(fbId) {
    if (!confirm("Remove your feedback?")) return;
    try {
      await apiFetch(`/api/feedbacks/${fbId}`, { method: "DELETE" });
      showToast("Feedback removed.");
      await loadFeedbacksCatalog();
      await loadAcademicFeedbacks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  /* --------------------------------------------------------------------------
     Shared Pagination Helper
     -------------------------------------------------------------------------- */

  function renderPagination(container, currentPage, totalPages, callbackName) {
    if (!container) return;
    if (!totalPages || totalPages <= 1) {
      container.innerHTML = "";
      return;
    }
    let html = "";
    if (currentPage > 1) {
      html += `<button class="btn-glass" onclick="${callbackName}(${currentPage - 1})">&larr; Prev</button>`;
    }
    for (let p = 1; p <= totalPages; p++) {
      if (p === currentPage) {
        html += `<button class="btn-illuminated" style="padding:7px 14px;">${p}</button>`;
      } else if (Math.abs(p - currentPage) <= 2 || p === 1 || p === totalPages) {
        html += `<button class="btn-glass" onclick="${callbackName}(${p})">${p}</button>`;
      }
    }
    if (currentPage < totalPages) {
      html += `<button class="btn-glass" onclick="${callbackName}(${currentPage + 1})">Next &rarr;</button>`;
    }
    container.innerHTML = html;
  }

  /* --------------------------------------------------------------------------
     PWA Service Worker & Add-to-Home-Screen Support
     -------------------------------------------------------------------------- */

  function initPwaSupport() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { });
    }

    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      state.deferredPwaPrompt = e;
      const btn = document.getElementById("pwaInstallBtn");
      if (btn) btn.style.display = "inline-flex";
    });

    window.addEventListener("appinstalled", () => {
      state.deferredPwaPrompt = null;
      const btn = document.getElementById("pwaInstallBtn");
      if (btn) btn.style.display = "none";
      showToast("AUA Hub installed to your device!");
    });
  }

  async function promptPwaInstall() {
    if (!state.deferredPwaPrompt) return;
    trackWebEvent("click_pwa_install");
    try {
      state.deferredPwaPrompt.prompt();
      await state.deferredPwaPrompt.userChoice;
    } catch { }
    state.deferredPwaPrompt = null;
    const btn = document.getElementById("pwaInstallBtn");
    if (btn) btn.style.display = "none";
  }

  /* --------------------------------------------------------------------------
     Bootstrap on DOMContentLoaded
     -------------------------------------------------------------------------- */

  window.addEventListener("DOMContentLoaded", async () => {
    initPwaSupport();
    await checkCurrentSession();
    initNavigationHistory();
    await loadCoursesCatalog(false);
    initSpamChatWebSocket();
  });
