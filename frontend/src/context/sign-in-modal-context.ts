import { createContext } from 'react'

export type SignInModalContextValue = {
  openSignIn: () => void
  closeSignIn: () => void
}

export const SignInModalContext = createContext<SignInModalContextValue | null>(
  null,
)
