SELECT DISTINCT
  p_a.email
FROM PERSON AS P_A
JOIN PERSON AS P_B
  ON p_a.email = p_b.email
WHERE
  p_a.id <> p_b.id