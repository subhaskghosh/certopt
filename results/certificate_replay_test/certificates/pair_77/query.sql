SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS AD
  ON ad.personid = p.personid