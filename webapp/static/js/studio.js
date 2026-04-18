/* 質心教育 XHS Studio — 单页前端
 * 4 个 tab (research / drafts / history / config) + 顶栏账号状态.
 * 所有数据通过 fetch 调本机 FastAPI; 无第三方框架.
 */
(function () {
  'use strict';

  // ---------- shared ----------
  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function toast(msg, type = 'info', timeout = 3500) {
    const box = $('toast');
    const el = document.createElement('div');
    el.className = 'toast-item ' + type;
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), timeout);
  }

  async function api(path, opts = {}) {
    const init = {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    const resp = await fetch(path, init);
    let data = null;
    try { data = await resp.json(); } catch (_) { data = { detail: await resp.text() }; }
    if (!resp.ok) {
      const msg = data && (data.detail || data.message) || `HTTP ${resp.status}`;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 10000) return (n / 10000).toFixed(1) + 'w';
    return String(n);
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  function withSpinner(btn, on) {
    if (!btn) return;
    if (on) {
      btn.dataset.label = btn.innerHTML;
      btn.innerHTML = '<span class="spinner"></span> 处理中…';
      btn.disabled = true;
    } else {
      if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
      btn.disabled = false;
    }
  }

  // ---------- tabs ----------
  const tabs = $$('#tabs button');
  const panels = $$('.tab-panel');
  tabs.forEach((b) => b.addEventListener('click', () => {
    tabs.forEach((x) => x.classList.toggle('active', x === b));
    const tab = b.dataset.tab;
    panels.forEach((p) => p.style.display = (p.id === 'tab-' + tab) ? '' : 'none');
    if (tab === 'drafts') loadDrafts();
    if (tab === 'history') loadHistory();
    if (tab === 'config') loadConfig();
  }));

  // ---------- account chip ----------
  // NOTE: 不再 setInterval 轮询! 每次 refreshAccount 都会让 xhs-mcp spawn Chromium.
  // 只在 a) 首次加载, b) 用户点「测试登录」时主动刷新.
  async function refreshAccount(force = false) {
    try {
      const d = await api('/api/account/status' + (force ? '?force=true' : ''));
      const dot = $('accountDot');
      const text = $('accountText');
      if (d.logged_in) {
        dot.className = 'dot green';
        let label = '已登录' + (d.username ? ' · ' + d.username : '');
        if (d.cached) label += ' (缓存)';
        text.textContent = label;
        text.title = d.cached ? `缓存 ${d.age}s 前; 点击刷新` : '点击刷新';
      } else {
        dot.className = 'dot red';
        text.textContent = d.message || '未登录';
        text.title = '点击刷新';
      }
    } catch (e) {
      $('accountDot').className = 'dot red';
      $('accountText').textContent = '账号检查失败';
    }
  }
  // 让账号 chip 可点击触发 force refresh
  const _accountChip = document.getElementById('accountChip');
  if (_accountChip) {
    _accountChip.style.cursor = 'pointer';
    _accountChip.addEventListener('click', () => refreshAccount(true));
  }

  // ===================================================================
  // RESEARCH TAB
  // ===================================================================
  let cards = [];           // 当前搜索结果
  let selected = new Set(); // 选中 feed_id 集合
  let lastDetails = [];     // 上次拉到的详情
  let lastBrief = null;     // 上次综合的 brief

  function renderCards() {
    const area = $('cardsArea');
    if (!cards.length) {
      area.innerHTML = '<div class="empty">没有结果, 试试更具体的关键词 (例: "DSE 中文 5**")</div>';
      $('selectedCount').textContent = '已选 0';
      $('fetchDetailsBtn').disabled = true;
      $('briefBtn').disabled = true;
      return;
    }
    area.innerHTML = cards.map((c) => {
      const sel = selected.has(c.feed_id);
      const cover = c.cover_url || '';
      return `
        <div class="note-card ${sel ? 'selected' : ''}" data-fid="${escapeHtml(c.feed_id)}">
          <div class="cover" style="background-image:url('${escapeHtml(cover)}');"></div>
          <div class="meta">
            <div class="title">${escapeHtml(c.title || '(无标题)')}</div>
            <div class="stats">
              <span>👤 ${escapeHtml(c.author || '')}</span>
              <span>👍 ${fmtNum(c.liked_count)}</span>
              <span>⭐ ${fmtNum(c.collected_count)}</span>
              <span>💬 ${fmtNum(c.comment_count)}</span>
            </div>
          </div>
        </div>
      `;
    }).join('');
    $$('.note-card', area).forEach((el) => {
      el.addEventListener('click', () => {
        const fid = el.dataset.fid;
        if (selected.has(fid)) selected.delete(fid); else selected.add(fid);
        renderCards();
      });
    });
    $('selectedCount').textContent = '已选 ' + selected.size;
    $('fetchDetailsBtn').disabled = selected.size === 0;
    $('briefBtn').disabled = selected.size === 0;
  }

  $('searchBtn').addEventListener('click', async () => {
    const kw = $('keyword').value.trim();
    if (!kw) return toast('请输入关键词', 'error');
    const btn = $('searchBtn');
    withSpinner(btn, true);
    try {
      const d = await api('/api/research/search', {
        method: 'POST',
        body: {
          keyword: kw,
          sort_by: $('sortBy').value,
          note_type: $('noteType').value,
          publish_time: $('publishTime').value,
          top_n: 12,
        },
      });
      cards = d.cards || [];
      selected.clear();
      renderCards();
      toast(`搜到 ${cards.length} 张笔记`, 'success');
    } catch (e) {
      toast('搜索失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  async function doFetchDetails() {
    const picks = cards.filter((c) => selected.has(c.feed_id))
      .map((c) => ({ feed_id: c.feed_id, xsec_token: c.xsec_token, title: c.title }));
    if (!picks.length) return null;
    const d = await api('/api/research/details', { method: 'POST', body: { picks } });
    lastDetails = d.details || [];
    return lastDetails;
  }

  $('fetchDetailsBtn').addEventListener('click', async () => {
    const btn = $('fetchDetailsBtn');
    withSpinner(btn, true);
    try {
      const dets = await doFetchDetails();
      toast(`已拉到 ${dets.length} 篇详情, 可以生成配方了`, 'success');
    } catch (e) {
      toast('拉详情失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  function renderBrief(b) {
    if (!b) return '';
    const list = (arr) => (arr && arr.length)
      ? '<ul>' + arr.map((x) => `<li>${escapeHtml(x)}</li>`).join('') + '</ul>'
      : '<div style="color:#86868b;">(无)</div>';
    const tagPills = (arr) => (arr || []).map((t) =>
      `<span class="pill">#${escapeHtml(String(t).replace(/^#/, ''))}</span>`).join('');
    return `
      <div class="label">📋 raw_research_summary</div>
      <div>${escapeHtml(b.raw_research_summary || '(无)')}</div>
      <div class="label">🎯 标题套路</div>${list(b.title_patterns)}
      <div class="label">🪝 开头钩子</div>${list(b.hooks)}
      <div class="label">🧩 正文结构</div>
      <div>${escapeHtml(b.structure_outline || '(无)')}</div>
      <div class="label">📏 推荐字数 / 图片</div>
      <div>${escapeHtml(b.recommended_word_count || '')} · 图片 ${b.recommended_image_count || 0} 张</div>
      <div class="label">🏷️ 推荐 tag</div>
      <div>${tagPills(b.recommended_tags)}</div>
      <div class="label">🔥 高频词</div>
      <div>${tagPills(b.viral_keywords)}</div>
      <div class="label">⚠️ 必须事实核查</div>${list(b.facts_to_verify)}
      <div class="label">💎 質心可差异化突出</div>${list(b.selling_points)}
      <div class="label">🚫 必须避开</div>${list(b.avoid_list)}
      <div class="label" style="font-size:11px; margin-top:12px; color:#999;">基于 ${(b.source_note_ids || []).length} 篇高赞笔记综合</div>
    `;
  }

  $('briefBtn').addEventListener('click', async () => {
    const btn = $('briefBtn');
    const topic = $('keyword').value.trim();
    const subject = $('subject').value;
    if (!topic) return toast('请先输入主题/关键词', 'error');
    if (!selected.size) return toast('请勾选至少 1 篇参考笔记', 'error');

    withSpinner(btn, true);
    try {
      // 如果还没拉详情, 先拉
      let details = lastDetails;
      const needRefetch = !details.length ||
        Array.from(selected).some((fid) => !details.find((d) => d.feed_id === fid));
      if (needRefetch) {
        toast('先拉详情…', 'info', 2000);
        details = await doFetchDetails();
      }
      const d = await api('/api/research/brief', {
        method: 'POST',
        body: { topic, subject, angle: 'soft_dry_goods', details },
      });
      lastBrief = d.brief;
      $('briefCard').style.display = '';
      $('briefArea').innerHTML = renderBrief(lastBrief);
      $('briefCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
      toast('爆款配方已出, 可以生成草稿了', 'success');
    } catch (e) {
      toast('生成配方失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  $('genDraftBtn').addEventListener('click', async () => {
    if (!lastBrief) return toast('请先生成 Brief', 'error');
    const btn = $('genDraftBtn');
    withSpinner(btn, true);
    try {
      const d = await api('/api/draft/generate', {
        method: 'POST',
        body: { brief: lastBrief, extra_instructions: $('extraInstr').value.trim() },
      });
      toast('草稿已生成, 转到 草稿 tab 审核', 'success');
      // 切到 drafts tab 并选中新草稿
      tabs.find((b) => b.dataset.tab === 'drafts').click();
      setTimeout(() => selectDraft(d.draft && d.draft.id), 400);
    } catch (e) {
      toast('生成草稿失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  // ===================================================================
  // DRAFTS TAB
  // ===================================================================
  let drafts = [];
  let activeDraftId = null;

  async function loadDrafts() {
    try {
      const d = await api('/api/drafts');
      drafts = d.drafts || [];
      renderDraftList();
      if (activeDraftId && drafts.find((x) => x.id === activeDraftId)) {
        selectDraft(activeDraftId);
      } else if (drafts.length) {
        selectDraft(drafts[0].id);
      } else {
        $('draftEditor').innerHTML = '<div class="empty">暂无草稿, 去研究 tab 生成</div>';
      }
    } catch (e) {
      toast('加载草稿失败: ' + e.message, 'error');
    }
  }

  // 批量选中的 draft id
  const __selectedDraftIds = new Set();

  function _updateBatchPubBtn() {
    const btn = document.getElementById('batch_publish_btn');
    if (!btn) return;
    const n = __selectedDraftIds.size;
    btn.disabled = n === 0;
    btn.textContent = `🚀 批量发布选中 (${n})`;
  }

  function renderDraftList() {
    const list = $('draftList');
    if (!drafts.length) {
      list.innerHTML = '<div class="empty">暂无草稿</div>';
      _updateBatchPubBtn();
      return;
    }
    // 清掉已不存在的选中
    const liveIds = new Set(drafts.map((d) => d.id));
    [...__selectedDraftIds].forEach((id) => { if (!liveIds.has(id)) __selectedDraftIds.delete(id); });

    list.innerHTML = drafts.map((d) => {
      const checked = __selectedDraftIds.has(d.id) ? 'checked' : '';
      const nImgs = ((d.image_urls || []).length) || ((d.images || []).length);
      return `
      <div class="draft-item ${d.id === activeDraftId ? 'active' : ''}" data-id="${escapeHtml(d.id)}">
        <div style="display:flex; align-items:flex-start; gap:6px;">
          <input type="checkbox" class="draft-pick" data-id="${escapeHtml(d.id)}" ${checked} onclick="event.stopPropagation()" />
          <div style="flex:1;">
            <div class="draft-title">${escapeHtml(d.title || d.topic || '(无标题)')}</div>
            <div class="draft-meta">
              ${escapeHtml(d.subject || '')}
              · ${escapeHtml((d.created_at || '').slice(0, 16).replace('T', ' '))}
              · ${(d.tags || []).length} tag · ${nImgs} 图
            </div>
          </div>
        </div>
      </div>`;
    }).join('');
    $$('.draft-item', list).forEach((el) => {
      el.addEventListener('click', (ev) => {
        if (ev.target && ev.target.classList && ev.target.classList.contains('draft-pick')) return;
        selectDraft(el.dataset.id);
      });
    });
    $$('.draft-pick', list).forEach((cb) => {
      cb.addEventListener('change', (ev) => {
        const id = ev.target.dataset.id;
        if (ev.target.checked) __selectedDraftIds.add(id); else __selectedDraftIds.delete(id);
        _updateBatchPubBtn();
      });
    });
    _updateBatchPubBtn();
  }

  function selectDraft(id) {
    if (!id) return;
    activeDraftId = id;
    renderDraftList();
    const d = drafts.find((x) => x.id === id);
    if (!d) {
      $('draftEditor').innerHTML = '<div class="empty">草稿不存在</div>';
      return;
    }
    renderDraftEditor(d);
  }

  function highlightFactLines(content, factLines) {
    const fl = new Set((factLines || []).map(Number));
    if (!fl.size) return escapeHtml(content);
    return content.split('\n').map((line, i) => {
      const safe = escapeHtml(line) || '&nbsp;';
      // 高亮: 事实行 + 仍未补 source 的 TODO 行
      let cls = '';
      if (fl.has(i)) cls = 'fact-line';
      if (line.includes('TODO_MANUAL')) cls = 'fact-line todo-source';
      return cls ? `<span class="${cls}">${safe}</span>` : safe;
    }).join('<br/>');
  }

  // 把 draft.images (本地绝对路径) + image_urls (cache/images/xxx) 渲染成缩略图网格
  function renderImageThumbnails(d) {
    const urls = [];
    // 优先用 image_urls (workflow 自动生成时填的)
    if (Array.isArray(d.image_urls)) {
      d.image_urls.forEach((u) => { if (u) urls.push(u); });
    }
    // 再加 d.images 里能转成 web 可见 URL 的部分 (cache/images/...)
    (d.images || []).forEach((p) => {
      if (!p) return;
      if (/^https?:\/\//i.test(p)) {
        urls.push(p);
      } else {
        // 提取 cache/images/<rest> 段
        const m = String(p).match(/cache\/images\/.+$/);
        if (m) urls.push('/' + m[0]);
      }
    });
    if (!urls.length) return '';
    const items = urls.map((u, i) => {
      const role = i === 0 ? '封面' : `配图 ${i}`;
      return `<div class="thumb-card">
        <a href="${escapeHtml(u)}" target="_blank">
          <img src="${escapeHtml(u)}" alt="${role}" />
        </a>
        <div class="thumb-label">${role}</div>
      </div>`;
    }).join('');
    return `<div class="thumb-grid" style="display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:8px; margin:8px 0 14px 0;">${items}</div>`;
  }

  function renderDraftEditor(d) {
    const tagsStr = (d.tags || []).map((t) => '#' + String(t).replace(/^#/, '')).join(' ');
    const imgsStr = (d.images || []).join('\n');
    const briefSummary = d.brief ? `
      <details style="margin-top:8px;">
        <summary style="cursor:pointer; color:#424245; font-size:12px;">📋 关联 Brief (${(d.brief.source_note_ids || []).length} 篇参考)</summary>
        <div class="brief-panel" style="margin-top:8px; font-size:12px;">${renderBrief(d.brief)}</div>
      </details>
    ` : '';

    $('draftEditor').innerHTML = `
      <div class="toolbar">
        <span class="pill draft">草稿</span>
        <span class="pill">${escapeHtml(d.subject || '通用')}</span>
        <span class="pill">${(d.content || '').length} 字</span>
        <span style="flex:1;"></span>
        <button id="d_regenBtn">🔁 重新生成</button>
        <button class="danger" id="d_delBtn">🗑️ 删除</button>
        <button class="primary" id="d_pubBtn">🚀 发布到小红书</button>
      </div>

      <label class="field">
        <span>标题 (12-22 字)</span>
        <input id="d_title" value="${escapeHtml(d.title || '')}" />
      </label>

      <label class="field">
        <span>正文 (黄色 = 待事实核查)</span>
        <textarea id="d_content" class="content-edit">${escapeHtml(d.content || '')}</textarea>
      </label>
      <div style="background:#fafafb; border-radius:8px; padding:10px 14px; font-size:13px; line-height:1.7; margin-bottom:12px;">
        <div style="font-size:11px; color:#86868b; margin-bottom:4px;">↓ 实时预览 (含事实段高亮)</div>
        <div id="d_preview">${highlightFactLines(d.content || '', d.fact_lines)}</div>
      </div>

      <label class="field">
        <span>标签 (空格分隔, 自动加 #)</span>
        <input id="d_tags" value="${escapeHtml(tagsStr)}" />
      </label>

      <label class="field">
        <span>图片 (本地绝对路径或 https URL, 一行一张, 至少 1 张)</span>
        <textarea id="d_images" rows="4" placeholder="/Users/you/Pictures/cover.jpg&#10;https://...">${escapeHtml(imgsStr)}</textarea>
      </label>

      ${renderImageThumbnails(d)}

      ${d.cover_concept ? `<div style="background:#fff4d6; padding:8px 12px; border-radius:8px; font-size:12px; margin-bottom:12px;">
        <strong>🎨 封面建议:</strong> ${escapeHtml(d.cover_concept)}
      </div>` : ''}
      ${d.notes_for_reviewer ? `<div style="background:#e6f3ff; padding:8px 12px; border-radius:8px; font-size:12px; margin-bottom:12px;">
        <strong>📝 给审稿同事:</strong> ${escapeHtml(d.notes_for_reviewer)}
      </div>` : ''}

      <div class="toolbar">
        <button id="d_saveBtn">💾 保存修改</button>
      </div>

      ${briefSummary}
    `;

    // realtime preview
    $('d_content').addEventListener('input', () => {
      $('d_preview').innerHTML = highlightFactLines($('d_content').value, d.fact_lines);
    });

    $('d_saveBtn').addEventListener('click', async () => {
      const btn = $('d_saveBtn');
      withSpinner(btn, true);
      try {
        const patch = {
          title: $('d_title').value.trim(),
          content: $('d_content').value,
          tags: $('d_tags').value.split(/\s+/).map((t) => t.replace(/^#/, '').trim()).filter(Boolean),
          images: $('d_images').value.split(/\n+/).map((s) => s.trim()).filter(Boolean),
        };
        const r = await api(`/api/draft/${encodeURIComponent(d.id)}`, { method: 'PATCH', body: patch });
        const idx = drafts.findIndex((x) => x.id === d.id);
        if (idx >= 0) drafts[idx] = r.draft;
        renderDraftList();
        renderDraftEditor(r.draft);
        toast('已保存', 'success');
      } catch (e) {
        toast('保存失败: ' + e.message, 'error');
      } finally {
        withSpinner(btn, false);
      }
    });

    $('d_delBtn').addEventListener('click', async () => {
      if (!confirm('确定删除这条草稿?')) return;
      try {
        await api(`/api/draft/${encodeURIComponent(d.id)}`, { method: 'DELETE' });
        toast('已删除', 'success');
        activeDraftId = null;
        await loadDrafts();
      } catch (e) {
        toast('删除失败: ' + e.message, 'error');
      }
    });

    $('d_regenBtn').addEventListener('click', async () => {
      if (!d.brief) return toast('该草稿缺 brief, 无法重生成 (请去研究 tab 重新走一遍)', 'error');
      const btn = $('d_regenBtn');
      withSpinner(btn, true);
      try {
        const extra = prompt('给写手的额外指示 (可空):', '') || '';
        const r = await api('/api/draft/generate', {
          method: 'POST', body: { brief: d.brief, extra_instructions: extra },
        });
        toast('已生成新版本草稿', 'success');
        await loadDrafts();
        if (r.draft && r.draft.id) selectDraft(r.draft.id);
      } catch (e) {
        toast('重生成失败: ' + e.message, 'error', 6000);
      } finally {
        withSpinner(btn, false);
      }
    });

    $('d_pubBtn').addEventListener('click', async () => {
      const images = $('d_images').value.split(/\n+/).map((s) => s.trim()).filter(Boolean);
      if (!images.length) return toast('请至少给 1 张图片再发布', 'error');
      if (!confirm(`确认发布到小红书?\n\n标题: ${$('d_title').value}\n图片: ${images.length} 张`)) return;
      // 先保存
      try {
        await api(`/api/draft/${encodeURIComponent(d.id)}`, {
          method: 'PATCH',
          body: {
            title: $('d_title').value.trim(),
            content: $('d_content').value,
            tags: $('d_tags').value.split(/\s+/).map((t) => t.replace(/^#/, '').trim()).filter(Boolean),
            images,
          },
        });
      } catch (e) {
        return toast('保存失败, 取消发布: ' + e.message, 'error');
      }
      const btn = $('d_pubBtn');
      withSpinner(btn, true);
      try {
        const r = await api(`/api/draft/${encodeURIComponent(d.id)}/publish`, {
          method: 'POST', body: { images },
        });
        toast('🎉 已发布到小红书!', 'success', 5000);
        activeDraftId = null;
        await loadDrafts();
      } catch (e) {
        toast('发布失败: ' + e.message, 'error', 8000);
      } finally {
        withSpinner(btn, false);
      }
    });
  }

  $('reloadDraftsBtn').addEventListener('click', loadDrafts);

  // 全选 checkbox
  const __selAllEl = document.getElementById('draft_select_all');
  if (__selAllEl) {
    __selAllEl.addEventListener('change', (ev) => {
      const on = ev.target.checked;
      drafts.forEach((d) => { if (on) __selectedDraftIds.add(d.id); else __selectedDraftIds.delete(d.id); });
      renderDraftList();
    });
  }

  // 批量发布
  const __batchPubBtn = document.getElementById('batch_publish_btn');
  if (__batchPubBtn) {
    __batchPubBtn.addEventListener('click', async () => {
      const ids = [...__selectedDraftIds];
      if (!ids.length) return;
      if (!confirm(`确定批量发布 ${ids.length} 条草稿到小红书? (会按 6 秒间隔串行发, 避免限流)`)) return;
      withSpinner(__batchPubBtn, true);
      let ok = 0, fail = 0;
      for (let i = 0; i < ids.length; i++) {
        const id = ids[i];
        try {
          const d = drafts.find((x) => x.id === id);
          if (!d) { fail++; continue; }
          const images = (d.images || []).filter(Boolean);
          if (!images.length) {
            fail++;
            toast(`⚠️ ${i + 1}/${ids.length}: 「${d.title || d.topic}」没有图片, 跳过`, 'error', 4000);
            continue;
          }
          await api(`/api/draft/${encodeURIComponent(id)}/publish`, {
            method: 'POST', body: { images },
          });
          ok++;
          toast(`✅ ${i + 1}/${ids.length}: ${d.title || d.topic}`, 'success', 2000);
          __selectedDraftIds.delete(id);
        } catch (e) {
          fail++;
          toast(`❌ ${i + 1}/${ids.length}: ${e.message}`, 'error', 4000);
        }
        if (i < ids.length - 1) await new Promise((r) => setTimeout(r, 6000));
      }
      withSpinner(__batchPubBtn, false);
      toast(`批量完成: 成功 ${ok}, 失败 ${fail}`, fail ? 'error' : 'success', 6000);
      await loadDrafts();
    });
  }

  // ===================================================================
  // HISTORY TAB
  // ===================================================================
  async function loadHistory() {
    const status = $('historyStatus').value;
    try {
      const url = status ? `/api/history?status=${encodeURIComponent(status)}` : '/api/history';
      const d = await api(url);
      const tasks = d.data || [];
      if (!tasks.length) {
        $('historyArea').innerHTML = '<div class="empty">暂无记录</div>';
        return;
      }
      $('historyArea').innerHTML = `
        <table class="history">
          <thead><tr>
            <th>时间</th><th>主题</th><th>标题</th><th>状态</th><th>消息</th><th></th>
          </tr></thead>
          <tbody>
          ${tasks.map((t) => `
            <tr>
              <td>${escapeHtml((t.created_at || '').slice(0, 16).replace('T', ' '))}</td>
              <td>${escapeHtml(t.topic || '')}</td>
              <td>${escapeHtml((t.title || '').slice(0, 30))}</td>
              <td><span class="pill ${t.status}">${escapeHtml(t.status || '')}</span></td>
              <td style="color:#86868b;">${escapeHtml((t.message || '').slice(0, 60))}</td>
              <td><button class="danger" data-del="${escapeHtml(t.id)}">删除</button></td>
            </tr>
          `).join('')}
          </tbody>
        </table>
      `;
      $$('button[data-del]', $('historyArea')).forEach((b) => {
        b.addEventListener('click', async () => {
          if (!confirm('确定删除?')) return;
          try {
            await api(`/api/history/${encodeURIComponent(b.dataset.del)}`, { method: 'DELETE' });
            loadHistory();
          } catch (e) { toast('删除失败: ' + e.message, 'error'); }
        });
      });
    } catch (e) {
      toast('加载历史失败: ' + e.message, 'error');
    }
  }
  $('reloadHistoryBtn').addEventListener('click', loadHistory);
  $('historyStatus').addEventListener('change', loadHistory);

  // ===================================================================
  // CONFIG TAB
  // ===================================================================
  async function loadConfig() {
    try {
      const d = await api('/api/config');
      const c = d.config || {};
      $('cfg_llm_key').value = c.llm_api_key || '';
      $('cfg_base_url').value = c.openai_base_url || '';
      $('cfg_model').value = c.default_model || 'anthropic/claude-sonnet-4.5';
      $('cfg_tavily').value = c.tavily_api_key || '';
      $('cfg_jina').value = c.jina_api_key || '';
      $('cfg_xhs_url').value = c.xhs_mcp_url || 'http://localhost:18060/mcp';
    } catch (e) {
      toast('加载配置失败: ' + e.message, 'error');
    }
  }

  $('saveConfigBtn').addEventListener('click', async () => {
    const btn = $('saveConfigBtn');
    withSpinner(btn, true);
    try {
      // 含 * 的脱敏值后端会自动跳过
      await api('/api/config', {
        method: 'POST',
        body: {
          llm_api_key: $('cfg_llm_key').value.trim(),
          openai_base_url: $('cfg_base_url').value.trim(),
          default_model: $('cfg_model').value.trim(),
          tavily_api_key: $('cfg_tavily').value.trim(),
          jina_api_key: $('cfg_jina').value.trim(),
          xhs_mcp_url: $('cfg_xhs_url').value.trim(),
        },
      });
      toast('配置已保存, MCP 服务已重启', 'success');
      refreshAccount(true);
    } catch (e) {
      toast('保存失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  $('validateModelBtn').addEventListener('click', async () => {
    const btn = $('validateModelBtn');
    withSpinner(btn, true);
    try {
      const d = await api('/api/validate-model', {
        method: 'POST',
        body: {
          llm_api_key: $('cfg_llm_key').value.trim(),
          openai_base_url: $('cfg_base_url').value.trim(),
          model_name: $('cfg_model').value.trim(),
        },
      });
      toast(d.message || '模型可用', 'success');
    } catch (e) {
      toast('验证失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  $('testLoginBtn').addEventListener('click', async () => {
    const btn = $('testLoginBtn');
    withSpinner(btn, true);
    try {
      const d = await api('/api/test-login', {
        method: 'POST', body: { xhs_mcp_url: $('cfg_xhs_url').value.trim() },
      });
      toast(d.message || (d.logged_in ? '已登录' : '未登录'),
            d.logged_in ? 'success' : 'error');
      refreshAccount(true);
    } catch (e) {
      toast('测试失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(btn, false);
    }
  });

  // ===================================================================
  // AGENTS TAB · Multi-agent workflow + live SSE timeline
  // ===================================================================

  const AGENT_ORDER = ['trend_scout', 'strategist', 'writer', 'critic', 'reviser', 'cover_designer'];
  const AGENT_LABEL = {
    trend_scout:    { name: '🛰 洞察侦察兵', role: '搜小红书 + 拉详情', tools: 'xhs.*' },
    strategist:     { name: '🧭 内容策略师', role: '反向工程爆款配方',   tools: '—' },
    writer:         { name: '✍️ 文案写手',   role: '基于 Brief 起稿',     tools: '—' },
    critic:         { name: '🧐 品控审稿',   role: '红线 + 事实检查',     tools: 'web.search' },
    reviser:        { name: '🔁 文案修订',   role: '按 Critic 修订',     tools: '—' },
    cover_designer: { name: '🎨 封面设计师', role: '生成封面 + 配图',     tools: 'image.generate' },
  };

  function renderAgentCards(activeAgent = null) {
    const container = $('agentCards');
    container.innerHTML = AGENT_ORDER.map((aid) => {
      const lbl = AGENT_LABEL[aid];
      const status = (window.__agentStatus || {})[aid] || 'idle';
      return `
        <div class="agent-card ${status}" data-agent="${aid}">
          <div class="ac-name">${lbl.name}</div>
          <div class="ac-role">${escapeHtml(lbl.role)}</div>
          <div class="ac-status" data-status>${statusLabel(status)}</div>
          <div class="ac-role" style="font-size:10px; opacity:0.7;">tools: ${lbl.tools}</div>
        </div>
      `;
    }).join('');
  }
  function statusLabel(s) {
    return ({ idle: '待命', running: '⚙️ 工作中…', completed: '✅ 完成', failed: '❌ 失败' })[s] || s;
  }
  function setAgentStatus(aid, status, extra = '') {
    if (!window.__agentStatus) window.__agentStatus = {};
    window.__agentStatus[aid] = status;
    const card = document.querySelector(`.agent-card[data-agent="${aid}"]`);
    if (card) {
      card.classList.remove('idle', 'running', 'completed', 'failed');
      card.classList.add(status);
      const st = card.querySelector('[data-status]');
      if (st) st.textContent = (statusLabel(status) + (extra ? ' · ' + extra : ''));
    }
  }
  function resetAgentStatuses() {
    window.__agentStatus = {};
    AGENT_ORDER.forEach((a) => (window.__agentStatus[a] = 'idle'));
    renderAgentCards();
  }

  function timelineAppend(ev) {
    const tl = $('agentTimeline');
    if (tl.querySelector('.empty')) tl.innerHTML = '';
    const line = document.createElement('div');
    let extraClass = '';
    if (ev.type === 'critic_verdict') {
      extraClass = (ev.data && ev.data.passed) ? 'pass' : 'fail';
    }
    line.className = `ev ${ev.type} ${extraClass}`;
    const ts = (ev.ts || '').slice(11, 19);
    const agent = ev.agent_name || ev.agent_id || '';
    const iter = ev.iteration ? `iter ${ev.iteration}` : '';
    let dataLine = '';
    if (ev.type === 'tool_call' && ev.data && ev.data.args) {
      dataLine = '<div class="ev-data">args: ' + escapeHtml(JSON.stringify(ev.data.args).slice(0, 200)) + '</div>';
    } else if (ev.type === 'tool_result' && ev.data && ev.data.preview) {
      dataLine = '<div class="ev-data">' + escapeHtml(String(ev.data.preview).slice(0, 200)) + '</div>';
    } else if (ev.type === 'critic_verdict' && ev.data) {
      dataLine = '<div class="ev-data">passed=' + (ev.data.passed ? '✅' : '❌')
              + ' issues=' + (ev.data.n_issues || 0) + '</div>';
    } else if (ev.type === 'llm_response' && ev.data && ev.data.preview) {
      dataLine = '<div class="ev-data">' + escapeHtml(ev.data.preview.slice(0, 160)) + '…</div>';
    }
    line.innerHTML = `
      <div class="ev-head">
        <span class="ev-type">${ev.type}</span>
        ${agent ? '<span class="ev-agent">' + escapeHtml(agent) + '</span>' : ''}
        ${iter ? '<span class="ev-iter">' + iter + '</span>' : ''}
        <span class="ev-iter" style="margin-left:auto;">${ts}</span>
      </div>
      <div class="ev-summary">${escapeHtml(ev.summary || '')}</div>
      ${dataLine}
    `;
    tl.appendChild(line);
    tl.scrollTop = tl.scrollHeight;
  }

  function renderRunOutput(state) {
    const root = $('ag_outputArea');
    if (!state || Object.keys(state).length === 0) {
      root.innerHTML = '<div class="empty">暂无输出</div>';
      return;
    }
    const sections = [];
    if (state.research_pack) {
      const picks = state.research_pack.picks || [];
      sections.push(`
        <h3>🛰 research_pack · ${picks.length} 篇 picks</h3>
        <ul style="line-height:1.7; font-size:13px;">
          ${picks.map((p) => `<li>👍${fmtNum(p.liked_count)} <strong>${escapeHtml(p.title || '')}</strong> · ${escapeHtml(p.author || '')}</li>`).join('')}
        </ul>
      `);
    }
    if (state.brief) {
      const b = state.brief;
      sections.push(`
        <h3>🧭 brief · 爆款配方</h3>
        <div class="brief-panel" style="font-size:12px;">
          <div class="label">title 套路</div>
          <ul>${(b.title_patterns || []).map((x) => '<li>' + escapeHtml(x) + '</li>').join('')}</ul>
          <div class="label">钩子</div>
          <ul>${(b.hooks || []).map((x) => '<li>' + escapeHtml(x) + '</li>').join('')}</ul>
          <div class="label">tags</div>
          <div>${(b.recommended_tags || []).map((t) => '<span class="pill">' + escapeHtml(t) + '</span>').join('')}</div>
        </div>
      `);
    }
    if (state.critic_report) {
      const cr = state.critic_report;
      sections.push(`
        <h3>🧐 critic_report · ${cr.passed ? '✅ PASS' : '❌ FAIL'}</h3>
        ${(cr.issues || []).length ? '<div style="font-size:12px;"><strong>Issues:</strong><ul>' + cr.issues.map((i) => '<li><span class="pill error">' + escapeHtml(i.category || '') + '</span> ' + escapeHtml(i.message || '') + '</li>').join('') + '</ul></div>' : ''}
        ${(cr.warnings || []).length ? '<div style="font-size:12px;"><strong>Warnings:</strong><ul>' + cr.warnings.map((i) => '<li><span class="pill warn">' + escapeHtml(i.category || '') + '</span> ' + escapeHtml(i.message || '') + '</li>').join('') + '</ul></div>' : ''}
      `);
    }
    if (state.draft) {
      const d = state.draft;
      sections.push(`
        <h3>✍️ draft</h3>
        <div style="font-size:13px;">
          <strong>${escapeHtml(d.title || '')}</strong>
          <div style="color:#86868b; margin: 4px 0;">字数 ${(d.content || '').length} · fact_lines: [${(d.fact_lines || []).join(', ')}]</div>
          <pre style="white-space:pre-wrap; font-family:inherit; font-size:13px; line-height:1.7; max-height: 400px; overflow:auto; background:#fafbfc; border:1px solid #ececef; border-radius:8px; padding:12px;">${escapeHtml(d.content || '')}</pre>
          <div>${(d.tags || []).map((t) => '<span class="pill">' + escapeHtml(t) + '</span>').join('')}</div>
          ${d.notes_for_reviewer ? '<div style="margin-top:8px; padding:8px; background:#fff4d6; border-radius:8px; font-size:12px;"><strong>notes_for_reviewer:</strong> ' + escapeHtml(d.notes_for_reviewer) + '</div>' : ''}
          <div style="margin-top:8px;"><a href="/studio#tab-drafts" style="font-size:12px;">→ 去草稿 tab 编辑/发布</a></div>
        </div>
      `);
    }
    if (state.images) {
      const imgs = state.images;
      const urls = [];
      if (imgs.cover) {
        const u = (typeof imgs.cover === 'object') ? (imgs.cover.url || imgs.cover.path) : imgs.cover;
        if (u) urls.push({ role: '封面 (3:4)', url: u });
      }
      (imgs.body || []).forEach((b, i) => {
        const u = (typeof b === 'object') ? (b.url || b.path) : b;
        if (u) urls.push({ role: `配图 ${i + 1} (1:1)`, url: u });
      });
      if (urls.length) {
        sections.push(`
          <h3>🎨 images · ${urls.length} 张</h3>
          <div style="display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:8px;">
            ${urls.map((it) => {
              const isWeb = it.url.startsWith('http') || it.url.startsWith('/');
              const src = isWeb ? it.url : '/' + it.url.replace(/^.*?cache\/images\//, 'cache/images/');
              return `<div class="thumb-card">
                <a href="${escapeHtml(src)}" target="_blank">
                  <img src="${escapeHtml(src)}" alt="${escapeHtml(it.role)}" />
                </a>
                <div class="thumb-label">${escapeHtml(it.role)}</div>
              </div>`;
            }).join('')}
          </div>
        `);
      }
    }
    root.innerHTML = sections.join('<hr style="border:none; border-top:1px solid #ececef; margin: 16px 0;">');
  }

  // EventSource for live SSE
  let __evSource = null;
  function openEventStream(runId) {
    if (__evSource) { try { __evSource.close(); } catch (_) {} }
    __evSource = new EventSource('/api/workflow/stream/' + encodeURIComponent(runId));
    const onAny = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      timelineAppend(data);
      // 更新 agent 卡片状态
      if (data.type === 'agent_started' && data.agent_id) {
        setAgentStatus(data.agent_id, 'running');
      } else if (data.type === 'agent_completed' && data.agent_id) {
        setAgentStatus(data.agent_id, 'completed');
      } else if (data.type === 'agent_failed' && data.agent_id) {
        setAgentStatus(data.agent_id, 'failed');
      } else if (data.type === 'critic_verdict' && data.data) {
        setAgentStatus('critic', data.data.passed ? 'completed' : 'failed', `${data.data.n_issues} 个 issue`);
      } else if (data.type === 'revision_triggered' && data.agent_id) {
        setAgentStatus(data.agent_id, 'running', `第 ${data.iteration + 1} 轮修订`);
      } else if (data.type === 'run_completed' || data.type === 'run_failed') {
        // 拉一次最终 state
        api('/api/workflow/run/' + encodeURIComponent(runId)).then((d) => {
          renderRunOutput(d.state || {});
          $('ag_runBtn').disabled = false;
          withSpinner($('ag_runBtn'), false);
          toast(data.type === 'run_completed' ? '✅ workflow 完成' : '❌ workflow 失败',
                data.type === 'run_completed' ? 'success' : 'error');
        }).catch(() => {});
        try { __evSource.close(); } catch (_) {}
      }
    };
    [
      'run_started', 'run_completed', 'run_failed',
      'agent_started', 'agent_completed', 'agent_failed',
      'llm_call', 'llm_response',
      'tool_call', 'tool_result',
      'critic_verdict', 'revision_triggered', 'log',
    ].forEach((t) => __evSource.addEventListener(t, onAny));
    __evSource.onerror = (e) => {
      console.warn('SSE error', e);
    };
  }

  $('ag_runBtn').addEventListener('click', async () => {
    const btn = $('ag_runBtn');
    withSpinner(btn, true);
    $('agentTimeline').innerHTML = '';
    $('ag_outputArea').innerHTML = '<div class="empty">workflow 跑中…</div>';
    resetAgentStatuses();
    try {
      const d = await api('/api/workflow/run', {
        method: 'POST',
        body: {
          workflow: $('ag_workflow').value,
          inputs: {
            keyword: $('ag_keyword').value.trim(),
            topic: $('ag_topic').value.trim(),
            subject: $('ag_subject').value,
            angle: $('ag_angle').value,
            top_n: parseInt($('ag_topn').value, 10) || 3,
          },
          save_as_draft: true,
        },
      });
      if (!d.run_id) throw new Error('未返回 run_id');
      toast('Workflow 启动: ' + d.run_id);
      openEventStream(d.run_id);
    } catch (e) {
      toast('启动失败: ' + e.message, 'error', 6000);
      withSpinner(btn, false);
    }
  });

  // initial render of agent cards
  resetAgentStatuses();

  // ===================================================================
  // BATCH MODE — 一次跑多个 workflow
  // ===================================================================

  function _parseBatchItems(text, defaultSubject) {
    const items = [];
    text.split('\n').forEach((raw) => {
      const line = raw.trim();
      if (!line || line.startsWith('#')) return;
      // 格式: keyword | topic | subject | angle (后三段可省, 用 | 分隔)
      const parts = line.split('|').map((s) => s.trim()).filter((_, i, a) => i < 4 || a.length <= 4);
      const keyword = parts[0] || '';
      const topic = parts[1] || keyword;
      const subject = parts[2] || defaultSubject || '通用';
      const angle = parts[3] || 'soft_dry_goods';
      if (!keyword) return;
      items.push({ keyword, topic, subject, angle, top_n: 3 });
    });
    return items;
  }

  let __batchPollTimer = null;
  function _stopBatchPoll() {
    if (__batchPollTimer) { clearInterval(__batchPollTimer); __batchPollTimer = null; }
  }

  function _renderBatchStatus(payload) {
    const root = document.getElementById('batch_status');
    if (!root) return;
    const b = payload.batch || {};
    const runs = payload.runs || [];
    const cards = runs.map((r, i) => {
      const status = r.status || 'unknown';
      const elapsed = r.elapsed_sec ? `${Math.round(r.elapsed_sec)}s` : '…';
      const passed = r.critic_passed === true ? '✅' : (r.critic_passed === false ? '⚠️' : '');
      const pct = status === 'completed' ? 100 : (status === 'failed' ? 100 : (status === 'running' ? 60 : 5));
      const title = r.draft_title || `(item ${i + 1})`;
      return `
        <div class="batch-card ${status}">
          <div class="bc-title">${escapeHtml(title)}</div>
          <div class="bc-meta">${escapeHtml(r.run_id || '')}</div>
          <div class="bc-bar"><span style="width:${pct}%;"></span></div>
          <div class="bc-meta">${status} · ${elapsed} · ${r.n_images || 0} 图 ${passed}</div>
        </div>`;
    }).join('');
    root.innerHTML = `
      <div style="font-size:12px; margin-bottom: 8px;">
        <strong>${escapeHtml(b.batch_id || '')}</strong>
        · ${b.n_done || 0}/${b.n_total || 0} 完成
        · ${b.n_running || 0} 跑中
        · ${b.n_failed || 0} 失败
        · ${b.status === 'completed' ? '✅ 全部结束' : '⏳ 进行中'}
      </div>
      <div class="batch-grid">${cards}</div>
    `;
  }

  async function _pollBatch(batchId) {
    try {
      const d = await api('/api/workflow/batch/' + encodeURIComponent(batchId));
      _renderBatchStatus(d);
      if ((d.batch && d.batch.status) === 'completed') {
        _stopBatchPoll();
        toast(`📦 批量完成: ${d.batch.n_done}/${d.batch.n_total} (${d.batch.draft_ids ? d.batch.draft_ids.length : 0} 草稿入队), 去 草稿 tab 审核`, 'success', 8000);
        loadDrafts();
      }
    } catch (e) {
      console.warn('poll batch err', e);
    }
  }

  const __batchRunBtn = document.getElementById('batch_runBtn');
  if (__batchRunBtn) {
    __batchRunBtn.addEventListener('click', async () => {
      const text = (document.getElementById('batch_items') || {}).value || '';
      const items = _parseBatchItems(text, (document.getElementById('batch_default_subject') || {}).value);
      if (!items.length) return toast('没有解析出有效行 (每行一个主题, # 开头是注释)', 'error');
      const wf = (document.getElementById('batch_workflow') || {}).value || 'research_to_draft';
      const par = parseInt((document.getElementById('batch_parallel') || {}).value || '3', 10);
      if (!confirm(`确认启动批量? ${items.length} 个选题, 并发 ${par}, workflow=${wf}\n(预计 ${Math.ceil(items.length / par) * 3}-${Math.ceil(items.length / par) * 6} 分钟)`)) return;
      withSpinner(__batchRunBtn, true);
      try {
        const d = await api('/api/workflow/batch/run', {
          method: 'POST',
          body: { workflow: wf, items, max_parallel: par, save_as_draft: true },
        });
        toast(`📦 批量启动: ${d.batch_id} (${d.n_items} 个选题)`, 'success', 5000);
        _stopBatchPoll();
        await _pollBatch(d.batch_id);
        __batchPollTimer = setInterval(() => _pollBatch(d.batch_id), 3000);
      } catch (e) {
        toast('批量启动失败: ' + e.message, 'error', 6000);
      } finally {
        withSpinner(__batchRunBtn, false);
      }
    });
  }

  // ===================================================================
  // AGENT SPECS EDITOR (in CONFIG tab)
  // ===================================================================
  let __currentSpecs = [];
  let __brandPrefix = '';

  async function loadAgentSpecs() {
    const root = $('specsEditor');
    if (!root) return;
    root.innerHTML = '<div class="empty"><span class="spinner"></span> 加载中…</div>';
    try {
      const d = await api('/api/agents/specs');
      __currentSpecs = d.specs || [];
      __brandPrefix = d.brand_prefix || '';
      renderSpecsEditor();
    } catch (e) {
      root.innerHTML = '<div class="empty">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function renderSpecsEditor() {
    const root = $('specsEditor');
    if (!root) return;
    root.innerHTML = __currentSpecs.map((s, idx) => `
      <div class="spec-row" data-idx="${idx}">
        <div class="sr-head">
          <input data-f="name" value="${escapeHtml(s.name || '')}" style="width:160px;" />
          <span class="sr-id">id: ${escapeHtml(s.id)}</span>
          <span style="flex:1;"></span>
          <label style="display:inline-flex; align-items:center; gap:4px; font-size:12px; color:#86868b;">
            <input data-f="enabled" type="checkbox" ${s.enabled !== false ? 'checked' : ''} style="width:auto;" />
            启用
          </label>
        </div>
        <label class="field">
          <span>角色描述</span>
          <input data-f="role" value="${escapeHtml(s.role || '')}" />
        </label>
        <label class="field">
          <span>system prompt (这个 agent 的核心指令)</span>
          <textarea data-f="system_prompt">${escapeHtml(s.system_prompt || '')}</textarea>
        </label>
        <div class="sr-grid">
          <label class="field">
            <span>model (空 = 用全局默认)</span>
            <input data-f="model" value="${escapeHtml(s.model || '')}" placeholder="anthropic/claude-sonnet-4.5" />
          </label>
          <label class="field">
            <span>temperature</span>
            <input data-f="temperature" type="number" step="0.05" min="0" max="2" value="${s.temperature ?? 0.5}" />
          </label>
          <label class="field">
            <span>max_tokens</span>
            <input data-f="max_tokens" type="number" min="256" max="16000" value="${s.max_tokens ?? 4000}" />
          </label>
          <label class="field">
            <span>max_iterations (tool loop)</span>
            <input data-f="max_iterations" type="number" min="1" max="10" value="${s.max_iterations ?? 5}" />
          </label>
        </div>
        <label class="field">
          <span>工具 (逗号分隔, 例: xhs.search_feeds, web.search)</span>
          <input data-f="tools" value="${escapeHtml((s.tools || []).join(', '))}" />
        </label>
      </div>
    `).join('');
  }

  function readSpecsFromDOM() {
    const rows = $$('.spec-row', $('specsEditor'));
    return rows.map((row, idx) => {
      const orig = __currentSpecs[idx] || {};
      const get = (f) => {
        const el = row.querySelector(`[data-f="${f}"]`);
        if (!el) return orig[f];
        if (el.type === 'checkbox') return el.checked;
        if (el.type === 'number') return Number(el.value);
        return el.value;
      };
      const toolsRaw = get('tools') || '';
      return {
        ...orig,
        name: get('name') || orig.name,
        role: get('role') || orig.role,
        system_prompt: get('system_prompt') || orig.system_prompt,
        model: (get('model') || '').trim() || null,
        temperature: get('temperature'),
        max_tokens: get('max_tokens'),
        max_iterations: get('max_iterations'),
        tools: toolsRaw.split(',').map((x) => x.trim()).filter(Boolean),
        enabled: get('enabled'),
      };
    });
  }

  const reloadSpecsBtn = $('reloadSpecsBtn');
  if (reloadSpecsBtn) reloadSpecsBtn.addEventListener('click', loadAgentSpecs);
  const saveSpecsBtn = $('saveSpecsBtn');
  if (saveSpecsBtn) saveSpecsBtn.addEventListener('click', async () => {
    withSpinner(saveSpecsBtn, true);
    try {
      const specs = readSpecsFromDOM();
      const d = await api('/api/agents/specs', {
        method: 'POST',
        body: { specs, brand_prefix: __brandPrefix },
      });
      toast(`已保存 ${d.n_specs} 个 spec → ${d.path}`, 'success', 4000);
    } catch (e) {
      toast('保存失败: ' + e.message, 'error', 6000);
    } finally {
      withSpinner(saveSpecsBtn, false);
    }
  });

  // 当切到 config tab 时, 在 loadConfig 外再额外加载 agent specs
  tabs.forEach((b) => {
    if (b.dataset.tab === 'config') {
      b.addEventListener('click', () => loadAgentSpecs());
    }
  });

  // ---------- bootstrap ----------
  // 注意: 不再 setInterval 轮询; 见 refreshAccount 注释.
  refreshAccount();
})();
