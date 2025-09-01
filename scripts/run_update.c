#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    // Execute the update script using bash
    execl("/bin/bash", "bash", "/home/volumio/Quadify/quadify_autoupdate.sh", (char *)NULL);
    perror("execl failed");
    return 1;
}

