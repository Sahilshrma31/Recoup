/** Recoup wordmark.
 *
 * Razorpay's own mark is a bare geometric glyph — no tile, no container — set
 * beside a tight navy wordmark. Recoup follows the same construction: a brand
 * blue recovery loop, broken at the top-left where a navy arrowhead re-enters
 * it. The two-tone split (blue figure, navy head) mirrors the way Razorpay
 * pairs its blue mark with a navy wordmark, so the two logos sit together
 * without competing.
 */
export function RecoupMark({ size = 26 }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      role="img"
      aria-label="Recoup"
    >
      <path
        d="M22.5 6.7A11.4 11.4 0 1 1 12.1 5.3"
        stroke="var(--rzp-blue)"
        strokeWidth="4.2"
        strokeLinecap="round"
      />
      <path d="M18.7 2.9 12.9 10.3 9.5 0.9Z" fill="var(--rzp-navy)" />
    </svg>
  )
}

export function BrandLockup({ size = 26 }) {
  return (
    <>
      <RecoupMark size={size} />
      <span className="brand-name">Recoup</span>
    </>
  )
}
