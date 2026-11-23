import { useEffect, useState } from 'react'

import { getHealth } from '@/api/client'

/**
 * "Prices as of <date>" strip under the header, on every page.
 *
 * The date is /health's data_as_of: when the prices were collected, not when
 * the API was last rebuilt. The site can serve a fixed snapshot, so shoppers
 * need to see how old a price is before relying on it. Renders nothing if the
 * API is down or the build predates data_as_of.
 */
function formatDate(iso: string) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleDateString('en-AU', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'Australia/Sydney',
  })
}

export default function PricesAsOf() {
  const [asOf, setAsOf] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    getHealth(controller.signal)
      .then((health) => {
        setAsOf(health.data_as_of ? formatDate(health.data_as_of) : null)
      })
      .catch(() => {
        // Decoration only: a failed health check just hides the strip.
      })

    return () => controller.abort()
  }, [])

  if (!asOf) return null

  return (
    <div className="border-b border-bw-line bg-bw-panel">
      <p className="px-6 py-2 text-xs text-bw-muted lg:px-8 xl:px-12 2xl:px-16">
        <span className="font-semibold text-bw-ink">Prices as of {asOf}.</span>{' '}
        They may have changed since, so check with the store before you buy.
      </p>
    </div>
  )
}
