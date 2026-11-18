package com.aimlegit.offhand;

import org.bukkit.entity.Player;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.List;

public final class Offhand extends JavaPlugin implements CommandExecutor {

    private Field spamExclusionsEnabledField;
    private Field spamExclusionsField;
    private boolean previousSpamExclusionsEnabled;
    private Object previousSpamExclusions;
    private List<String> activeSpamExclusions;

    @Override
    public void onEnable() {
        getCommand("offhand").setExecutor(this);
        enableSpamExclusion();
    }

    @Override
    public void onDisable() {
        restoreSpamExclusion();
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage("Este comando solo puede usarlo un jugador.");
            return true;
        }

        if (args.length != 0) {
            player.sendMessage("Uso: /offhand");
            return true;
        }

        swapHands(player);
        return true;
    }

    private void enableSpamExclusion() {
        try {
            Class<?> spigotConfig = Class.forName("org.spigotmc.SpigotConfig");
            spamExclusionsEnabledField = spigotConfig.getField("enableSpamExclusions");
            spamExclusionsField = spigotConfig.getField("spamExclusions");

            previousSpamExclusionsEnabled = spamExclusionsEnabledField.getBoolean(null);
            previousSpamExclusions = spamExclusionsField.get(null);

            activeSpamExclusions = new ArrayList<>();
            if (previousSpamExclusionsEnabled && previousSpamExclusions instanceof List<?> exclusions) {
                for (Object exclusion : exclusions) {
                    if (exclusion instanceof String value) {
                        activeSpamExclusions.add(value);
                    }
                }
            }
            if (!activeSpamExclusions.contains("/offhand")) {
                activeSpamExclusions.add("/offhand");
            }

            spamExclusionsField.set(null, activeSpamExclusions);
            spamExclusionsEnabledField.setBoolean(null, true);
            getLogger().info("/offhand excluido del filtro anti-spam de Paper.");
        } catch (ReflectiveOperationException | RuntimeException exception) {
            getLogger().warning("No se pudo excluir /offhand del filtro anti-spam de Paper: "
                    + exception.getClass().getSimpleName());
        }
    }

    private void restoreSpamExclusion() {
        if (spamExclusionsField == null || activeSpamExclusions == null) {
            return;
        }

        try {
            if (spamExclusionsField.get(null) == activeSpamExclusions) {
                spamExclusionsField.set(null, previousSpamExclusions);
                spamExclusionsEnabledField.setBoolean(null, previousSpamExclusionsEnabled);
            }
        } catch (ReflectiveOperationException | RuntimeException exception) {
            getLogger().warning("No se pudo restaurar la configuración anti-spam de Paper: "
                    + exception.getClass().getSimpleName());
        }
    }

    private void swapHands(Player player) {
        ItemStack mainHand = player.getInventory().getItemInMainHand().clone();
        ItemStack offHand = player.getInventory().getItemInOffHand().clone();

        // Similar items have the same type and metadata, regardless of stack size.
        if (mainHand.isSimilar(offHand)) {
            return;
        }

        player.getInventory().setItemInMainHand(offHand);
        player.getInventory().setItemInOffHand(mainHand);
    }
}
