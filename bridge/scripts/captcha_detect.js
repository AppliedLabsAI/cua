/**
 * CAPTCHA detection for CUA agent.
 *
 * Single DOM query that detects Cloudflare, reCAPTCHA, and hCaptcha patterns.
 * Returns { type, blocking } or null if no CAPTCHA found.
 *
 * Registered as window.__detectCaptcha via add_init_script.
 */
window.__detectCaptcha = () => {
  // Cloudflare challenge page (full-page interstitial)
  if (
    document.title.includes('Just a moment') ||
    document.querySelector(
      '#challenge-running, #challenge-stage, .cf-challenge',
    )
  ) {
    return { blocking: true, type: 'cloudflare' };
  }

  // Cloudflare Turnstile widget (inline, not full-page)
  if (
    document.querySelector(
      '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]',
    )
  ) {
    return { blocking: false, type: 'cloudflare' };
  }

  // reCAPTCHA v2/v3
  if (
    document.querySelector(
      '.g-recaptcha, iframe[src*="recaptcha/api2"], iframe[src*="recaptcha/enterprise"]',
    )
  ) {
    return { blocking: false, type: 'recaptcha' };
  }

  // hCaptcha
  if (document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"]')) {
    return { blocking: false, type: 'hcaptcha' };
  }

  return null;
};

/**
 * Check if a CAPTCHA is still present (for polling during wait).
 */
window.__captchaStillPresent = () => {
  if (document.title.includes('Just a moment')) return true;
  if (
    document.querySelector(
      '#challenge-running, #challenge-stage, .cf-challenge',
    )
  )
    return true;
  if (
    document.querySelector(
      '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]',
    )
  )
    return true;
  if (document.querySelector('.g-recaptcha, iframe[src*="recaptcha"]'))
    return true;
  if (document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"]'))
    return true;
  return false;
};
