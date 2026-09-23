/* 网易云 Discord 状态 — landing page 交互
   视差 / 滚动场景 / 进场动画 / 状态卡片演示，全部原生 JS */
(() => {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  };

  // 演示歌曲: Stay (02:21)。不展示真实歌词，歌词位显示歌曲段落 (时间为示意)
  const DURATION = 141; // 02:21
  const LYRICS = [
    [0, "Intro"],
    [12, "Verse 1"],
    [30, "Pre-Chorus"],
    [42, "Chorus"],
    [66, "Verse 2"],
    [90, "Chorus"],
    [118, "Outro"],
  ];
  const lyricAt = (t) => {
    let line = LYRICS[0][1];
    for (const [time, text] of LYRICS) if (t >= time) line = text;
    return line;
  };

  /* ---------------- 导航背景 ---------------- */
  const nav = document.getElementById("nav");

  /* ---------------- 进场动画 ---------------- */
  const reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && !reduceMotion) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      }
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
    reveals.forEach((el) => io.observe(el));
  } else {
    reveals.forEach((el) => el.classList.add("in"));
  }

  /* ---------------- 视差 ---------------- */
  // data-max: 位移上限 (px)，用于卡片内的装饰数字，避免压到文字
  const layers = [...document.querySelectorAll("[data-speed]")].map((el) => ({
    el, speed: parseFloat(el.dataset.speed) || 0, max: parseFloat(el.dataset.max) || Infinity,
  }));

  /* ---------------- 滚动场景 ---------------- */
  const sceneProgress = (section) => {
    const r = section.getBoundingClientRect();
    const total = r.height - window.innerHeight;
    return total > 0 ? clamp(-r.top / total, 0, 1) : 0;
  };

  const lyricScene = document.getElementById("lyrics");
  const lyricItems = [...document.querySelectorAll("#lyricList li")];
  let lastLyric = -1;

  const scrubScene = document.getElementById("scrub");
  const scrubTime = document.getElementById("scrubTime");
  const scrubCur = document.getElementById("scrubCur");
  const scrubFill = document.getElementById("scrubFill");
  const scrubLyric = document.getElementById("scrubLyric");

  function update() {
    const y = window.scrollY;
    const vh = window.innerHeight;
    nav.classList.toggle("scrolled", y > 10);

    if (!reduceMotion) {
      const strength = window.innerWidth < 640 ? 0.45 : 1; // 手机上视差减弱，避免元素互相压住
      for (const { el, speed, max } of layers) {
        const r = el.parentElement.getBoundingClientRect();
        if (r.bottom < -vh || r.top > vh * 2) continue; // 离屏时跳过
        const offset = clamp((r.top + r.height / 2 - vh / 2) * speed * strength, -max, max);
        el.style.transform = `translate3d(0, ${offset.toFixed(1)}px, 0)`;
      }
    }

    // 歌词逐句点亮
    const lp = sceneProgress(lyricScene);
    const idx = Math.min(lyricItems.length - 1, Math.floor(lp * lyricItems.length * 0.999));
    if (idx !== lastLyric) {
      lyricItems.forEach((li, i) => {
        li.classList.toggle("active", i === idx);
        li.classList.toggle("past", i < idx);
      });
      lastLyric = idx;
    }

    // 滚动 = 拖动进度条
    const sp = sceneProgress(scrubScene);
    const t = sp * DURATION;
    scrubTime.textContent = fmt(t);
    scrubCur.textContent = fmt(t);
    scrubFill.style.width = (sp * 100).toFixed(2) + "%";
    const line = "♪ " + lyricAt(t);
    if (scrubLyric.textContent !== line) scrubLyric.textContent = line;
  }

  let ticking = false;
  const onScroll = () => {
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(() => { update(); ticking = false; });
    }
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  update();

  /* ---------------- Hero 卡片：播放中 ---------------- */
  const heroCard = document.getElementById("heroCard");
  const heroCur = heroCard.querySelector("[data-cur]");
  const heroFill = heroCard.querySelector("[data-fill]");
  const heroLyric = heroCard.querySelector("[data-lyric]");
  let heroT = 14;

  function tickHero() {
    heroCur.textContent = fmt(heroT);
    heroFill.style.width = ((heroT / DURATION) * 100).toFixed(2) + "%";
    const line = "♪ " + lyricAt(heroT);
    if (heroLyric.textContent !== line) {
      heroLyric.classList.add("swap");
      setTimeout(() => { heroLyric.textContent = line; heroLyric.classList.remove("swap"); }, 300);
    }
  }
  // 演示用：进度 3 倍速走，让歌词更快轮换
  tickHero();
  setInterval(() => { heroT = (heroT + 3) % DURATION; tickHero(); }, 1000);

  /* ---------------- Bento：暂停 / 播放 循环 ---------------- */
  const pauseCard = document.getElementById("pauseCard");
  if (pauseCard) {
    const state = pauseCard.querySelector(".mini-state");
    let paused = false;
    setInterval(() => {
      paused = !paused;
      pauseCard.classList.toggle("paused", paused);
      state.textContent = paused ? state.dataset.paused : state.dataset.playing;
    }, 2600);
  }
})();
