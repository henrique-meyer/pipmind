(() => {
  const id = document.currentScript?.dataset.measurementId;
  if (!/^G-[A-Z0-9]{4,20}$/.test(id || "") || window.gtag) return;

  // Exclude query strings and fragments from page/referrer measurements.
  const cleanUrl = (value) => {
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) ? url.origin + url.pathname : "";
    } catch {
      return "";
    }
  };
  window.dataLayer = window.dataLayer || [];
  window.gtag = function () { window.dataLayer.push(arguments); };
  window.gtag("js", new Date());
  window.gtag("config", id, {
    page_location: cleanUrl(window.location.href),
    page_referrer: cleanUrl(document.referrer),
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
  });
  const script = document.createElement("script");
  script.async = true;
  script.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
  document.head.appendChild(script);
})();
