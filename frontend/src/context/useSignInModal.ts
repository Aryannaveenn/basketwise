import { useContext } from 'react'

import { SignInModalContext } from '@/context/sign-in-modal-context'

export function useSignInModal() {
  const context = useContext(SignInModalContext)

  if (!context) {
    throw new Error('useSignInModal must be used within a SignInModalProvider')
  }

  return context
}
