/**
 * Billing state for the browser — display only.
 *
 * Stripe is the source of truth and the server is the only thing that talks to
 * it: every value here came from /api/billing/status behind a verified Firebase
 * token, and nothing here grants access to anything.
 */

import { trackEvent } from '/analytics.js';
import { authFetch } from '/auth.js';

const state = { loading: true, error: '', data: null };
const listeners = new Set();

const snapshot = () => ({
  loading: state.loading,
  error: state.error,
  billingEnabled: !!(state.data && state.data.billingEnabled),
  isOwner: !!(state.data && state.data.isOwner),
  hasAccess: !!(state.data && state.data.hasAccess),
  gatedFeatures: (state.data && state.data.gatedFeatures) || [],
  status: state.data ? state.data.status : 'none',
  plan: state.data ? state.data.plan : '',
  planName: state.data ? state.data.planName : '',
  isActive: !!(state.data && state.data.isActive),
  inGrace: !!(state.data && state.data.inGrace),
  hasSubscription: !!(state.data && state.data.hasSubscription),
  cancelAtPeriodEnd: !!(state.data && state.data.cancelAtPeriodEnd),
  currentPeriodEnd: state.data ? state.data.currentPeriodEnd : null,
  plans: (state.data && state.data.plans) || [],
});

const notify = () => listeners.forEach(fn => fn(snapshot()));

export function onBilling(fn) {
  listeners.add(fn);
  fn(snapshot());
  return () => listeners.delete(fn);
}

export function getBilling() {
  return snapshot();
}

let inFlight = null;

/** Ask the server for the current Stripe state. */
export function refresh() {
  if (inFlight) return inFlight;
  state.loading = true;
  state.error = '';
  notify();
  inFlight = (async () => {
    try {
      const res = await authFetch('/api/billing/status');
      if (!res.ok) throw new Error(await detail(res));
      state.data = await res.json();
    } catch (e) {
      state.error = e.message || 'Could not load billing.';
    } finally {
      state.loading = false;
      inFlight = null;
      notify();
    }
    return snapshot();
  })();
  return inFlight;
}

async function detail(res) {
  const body = await res.json().catch(() => ({}));
  return body.detail || 'Billing is unavailable right now.';
}

async function post(url, body) {
  const res = await authFetch(url, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

/** Start Stripe Checkout for a plan key (never a price id). */
export async function subscribe(plan) {
  // Leaving for Checkout is an intent, never a subscription: only Stripe's
  // confirmed status counts, and that is reported from the success page.
  trackEvent('billing_period_selected', { billing_period: plan === 'yearly' ? 'annual' : 'monthly' });
  trackEvent('checkout_started', { billing_period: plan === 'yearly' ? 'annual' : 'monthly' });
  const { url } = await post('/api/billing/checkout', { plan });
  window.location.assign(url);
}

export async function openPortal() {
  trackEvent('billing_portal_opened', {});
  const { url } = await post('/api/billing/portal');
  window.location.assign(url);
}

export async function cancel() {
  trackEvent('subscription_cancel_requested', {});
  state.data = await post('/api/billing/cancel');
  notify();
  return snapshot();
}

export async function resume() {
  trackEvent('subscription_resumed', {});
  state.data = await post('/api/billing/resume');
  notify();
  return snapshot();
}

export const formatDate = seconds =>
  seconds
    ? new Date(seconds * 1000).toLocaleDateString(undefined, {
        year: 'numeric', month: 'long', day: 'numeric',
      })
    : '';
