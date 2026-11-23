import { Outlet } from 'react-router'

import Footer from '@/components/Footer'
import Header from '@/components/header/Header'
import PricesAsOf from '@/components/PricesAsOf'

export default function MainLayout() {
  return (
    <div className="flex min-h-svh flex-col">
      <Header />

      <PricesAsOf />

      <main className="flex-1">
        <Outlet />
      </main>

      <Footer />
    </div>
  )
}
