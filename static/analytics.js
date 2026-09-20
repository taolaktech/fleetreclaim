/**
 * Google Analytics 4 for the app — observational only.
 *
 * The measurement id comes from the server (/api/config) so it lives in one
 * place; without it nothing is loaded and every call here is a no-op. gtag is
 * loaded asynchronously and every failure is swallowed: analytics must never
 * break sign-in, uploads, processing or billing.
 *
 * Only behavioural and aggregate values are ever sent. Never pass names,
 * emails, uids, plates, trip ids, filenames, document text, amounts or Stripe
 * identifiers to trackEvent().
 */

// fleetreclaim.com and app.fleetreclaim.com share one GA4 property; the linker
// carries the client id across so the marketing visit and the app session stay
// one journey instead of the app looking like a referral from the landing page.
const LINKED_DOMAINS = ['fleetreclaim.com', 'app.fleetreclaim.com'];

let enabled = false;

function gtag() {
  window.dataLayer.push(arguments);
}

/** Load gtag.js once, if the server configured a measurement id. */
export function initAnalytics(measurementId) {
  if (enabled || !measurementId) return;
  try {
    window.dataLayer = window.dataLayer || [];
    const script = document.createElement('script');
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(measurementId)}`;
    document.head.appendChild(script);

    gtag('js', new Date());
    gtag('config', measurementId, { linker: { domains: LINKED_DOMAINS } });
    enabled = true;
  } catch (e) {
    enabled = false;
  }
}

/** Send one GA4 event. Safe to call before init, or when GA is blocked. */
export function trackEvent(name, parameters) {
  if (!enabled || !name) return;
  try {
    gtag('event', name, parameters || {});
  } catch (e) {
    /* analytics must never surface to the user */
  }
}

/** Coarse bucket for a failure, so no raw error text reaches GA. */
export function errorCategory(error, status) {
  if (status === 400) return 'invalid_file';
  if (status === 401 || status === 403) return 'auth_error';
  if (status === 402) return 'payment_required';
  if (status === 415) return 'unsupported_format';
  if (status >= 500) return 'server_error';
  if (error instanceof TypeError) return 'network_error';
  return 'unknown';
}

/** Extension bucket for an uploaded file: pdf, image, csv, xlsx or other. */
export function fileType(filename) {
  const ext = String(filename || '').split('.').pop().toLowerCase();
  if (ext === 'pdf') return 'pdf';
  if (['png', 'jpg', 'jpeg', 'webp', 'tif', 'tiff'].includes(ext)) return 'image';
  if (ext === 'csv') return 'csv';
  if (ext === 'xlsx' || ext === 'xls') return 'xlsx';
  return 'other';
}
