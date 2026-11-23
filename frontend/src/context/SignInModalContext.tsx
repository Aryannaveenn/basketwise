import { useCallback, useMemo, useState, type ReactNode } from 'react'

import { SignInModalContext } from '@/context/sign-in-modal-context'
import SignInModal from '@/pages/SignInPage'

// The hook lives in useSignInModal.ts so this file only exports a component
// (keeps React Fast Refresh working).
export function SignInModalProvider({ children }: { children: ReactNode }) {
  const [isSignInOpen, setIsSignInOpen] = useState(false)

  const openSignIn = useCallback(() => setIsSignInOpen(true), [])
  const closeSignIn = useCallback(() => setIsSignInOpen(false), [])

  const value = useMemo(
    () => ({ openSignIn, closeSignIn }),
    [openSignIn, closeSignIn],
  )

  return (
    <SignInModalContext.Provider value={value}>
      {children}

      <SignInModal isOpen={isSignInOpen} onClose={closeSignIn} />
    </SignInModalContext.Provider>
  )
}
