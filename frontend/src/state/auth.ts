import { create } from "zustand";

interface SignInState {
  /** Where to return to once the sign-in succeeds. */
  redirectTo: string | null;
  setRedirect: (path: string | null) => void;
}

export const useSignInStore = create<SignInState>((set) => ({
  redirectTo: null,
  setRedirect: (path) => set({ redirectTo: path }),
}));

export const resetSignIn = (): void => useSignInStore.setState({ redirectTo: null });
