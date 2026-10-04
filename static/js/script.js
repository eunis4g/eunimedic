const passwordRevealButtons = document.querySelectorAll(
    "[data-password-reveal]"
);

passwordRevealButtons.forEach((button) => {
    const passwordInput = document.getElementById(
        button.getAttribute("aria-controls")
    );

    if (!passwordInput) {
        return;
    }

    const showPassword = () => {
        passwordInput.type = "text";
    };

    const hidePassword = () => {
        passwordInput.type = "password";
    };

    button.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "mouse" && event.button !== 0) {
            return;
        }

        showPassword();
    });

    button.addEventListener("pointerup", hidePassword);
    button.addEventListener("pointerleave", hidePassword);
    button.addEventListener("pointercancel", hidePassword);
    button.addEventListener("blur", hidePassword);

    button.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            showPassword();
        }
    });

    button.addEventListener("keyup", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            hidePassword();
        }
    });
});

const resendVerificationButton = document.getElementById(
    "resend-verification-button"
);

if (resendVerificationButton) {
    const initialRemainingSeconds = Number.parseInt(
        resendVerificationButton.dataset.remainingSeconds,
        10
    );

    if (
        Number.isFinite(initialRemainingSeconds)
        && initialRemainingSeconds > 0
    ) {
        const resendAvailableAt = (
            Date.now() + initialRemainingSeconds * 1000
        );

        const updateResendCountdown = () => {
            const remainingSeconds = Math.max(
                0,
                Math.ceil((resendAvailableAt - Date.now()) / 1000)
            );

            if (remainingSeconds === 0) {
                resendVerificationButton.disabled = false;
                resendVerificationButton.textContent = "인증메일 재전송";
                return true;
            }

            resendVerificationButton.disabled = true;
            resendVerificationButton.textContent = (
                `인증메일 재전송 (${remainingSeconds}초)`
            );
            return false;
        };

        updateResendCountdown();

        const countdownInterval = window.setInterval(() => {
            if (updateResendCountdown()) {
                window.clearInterval(countdownInterval);
            }
        }, 1000);
    } else {
        resendVerificationButton.disabled = false;
        resendVerificationButton.textContent = "인증메일 재전송";
    }
}
