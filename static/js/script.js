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
